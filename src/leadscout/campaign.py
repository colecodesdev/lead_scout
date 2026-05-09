"""Multi-day, multi-location campaign orchestration (feature 06).

A campaign is a TOML plan file listing locations + Places categories.
Each invocation of `leadscout campaign` processes one day's slice of
jobs, picks up where the previous day stopped, halts when the daily
Places quota safe limit is reached, and is idempotent (re-running on
the same day is a no-op skip when nothing has gone stale).

A "job" is a `(location, category)` pair. The plan file's [[jobs]]
arrays cross-product with their `categories` lists at load time, so
2 locations × 3 categories = 6 JobSpecs.

This module owns three responsibilities, none of which the pipeline
itself should know about:

1. **Plan parsing** — load TOML, validate categories against the
   curated allowlist, expand into JobSpec list.
2. **Daily slice selection** — skip (location, category) pairs that
   were fully refreshed within `refresh_days`.
3. **Quota-aware execution** — stop when PlacesQuotaTracker hits
   safe limit; never crash the campaign because one job hit a
   per-business APIError.

The actual pipeline work for each job delegates to
`leadscout.pipeline.run_pipeline`.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from leadscout.config import (
    BLOCKED_CHAIN_NAMES,
    CAMPAIGN_MIN_REVIEW_COUNT,
    CAMPAIGN_REFRESH_DAYS,
    DEFAULT_RADIUS,
    KNOWN_BUSINESS_TYPES,
    PLACES_SAFE_LIMIT,
    PLACES_WARN_THRESHOLD,
)
from leadscout.exceptions import APIError, LeadScoutError
from leadscout.pipeline import run_pipeline
from leadscout.places_quota import PlacesQuotaTracker
from leadscout.storage import data_path_for_location, load_data

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobSpec:
    """One unit of campaign work: a location + a single category.

    `frozen=True` because JobSpecs are dict-keys / set-members for
    fresh-skip bookkeeping in select_todays_jobs.
    """

    location: str
    category: str
    radius: int = DEFAULT_RADIUS


@dataclass
class CampaignSummary:
    """Returned from run_campaign. Printed by the CLI handler.

    Counts aren't strictly needed by the pipeline itself but make the
    CLI output meaningful at a glance and the tests easy to assert on.
    """

    jobs_total: int = 0
    jobs_run: int = 0
    jobs_skipped_fresh: int = 0
    jobs_remaining: int = 0
    halted_reason: str | None = None
    businesses_filtered_dead: int = 0
    businesses_filtered_chain: int = 0
    # Places quota state at the end of the run. Helpful for the user
    # to see how close they came to the cap.
    places_quota_used: int = 0
    places_quota_safe_limit: int = 0
    # Each entry: (location, category) of an unrun job. Useful for the
    # log line that explains "these will run tomorrow."
    queued_jobs: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Plan loading.
# ---------------------------------------------------------------------------


def load_plan(path: Path) -> tuple[list[JobSpec], dict]:
    """Parse a TOML plan file into JobSpec list + defaults dict.

    Plan shape:

        [defaults]
        radius = 5000
        min_review_count = 1

        [[jobs]]
        location = "Santa Rosa Beach, FL"
        categories = ["restaurant", "dentist"]

        [[jobs]]
        location = "Destin, FL"
        categories = ["gym", "spa"]

    Cross-products `(location, categories[i])` for every job. So the
    above plan expands to 4 JobSpecs.

    Validates each category against `KNOWN_BUSINESS_TYPES`. Unknown
    categories log a warning but still produce a JobSpec — the
    allowlist is curatorial, not a hard validator. This lets a user
    try a new Places type without first editing config.py.

    Returns:
        (jobs, defaults). The defaults dict is passed through to
        run_campaign so per-job tunables (min_review_count etc.)
        can override module-level CAMPAIGN_* constants.

    Raises:
        FileNotFoundError if the plan file doesn't exist (caller
            should surface a friendly message).
        LeadScoutError on a malformed plan (missing required keys,
            invalid types).
    """
    if not path.exists():
        raise FileNotFoundError(f"Campaign plan not found: {path}")

    # `tomllib.loads` requires a bytes input; read in binary mode and
    # decode happens inside the parser. This is the stdlib API since
    # 3.11, so no third-party dep.
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise LeadScoutError(f"Failed to parse campaign plan {path}: {e}") from e

    # Defaults table is optional. Empty {} when absent so callers can
    # always .get() without needing a None branch.
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise LeadScoutError(
            f"[defaults] must be a table, got {type(defaults).__name__}"
        )

    # Per-defaults radius value. Falling back to DEFAULT_RADIUS keeps
    # `radius = 5000` optional in the plan file.
    default_radius = int(defaults.get("radius", DEFAULT_RADIUS))

    raw_jobs = data.get("jobs") or []
    if not isinstance(raw_jobs, list):
        raise LeadScoutError("[[jobs]] must be an array of tables")

    jobs: list[JobSpec] = []
    # Track unknowns once per category so we don't spam warnings for
    # every reuse of the same unknown type across jobs.
    warned_unknowns: set[str] = set()
    for entry in raw_jobs:
        if not isinstance(entry, dict):
            raise LeadScoutError(
                f"Each [[jobs]] entry must be a table, got {type(entry).__name__}"
            )
        location = entry.get("location")
        categories = entry.get("categories")
        if not location or not isinstance(location, str):
            raise LeadScoutError(
                f"Each [[jobs]] entry needs a `location` string; got {entry!r}"
            )
        if not categories or not isinstance(categories, list):
            raise LeadScoutError(
                f"Each [[jobs]] entry needs a `categories` array; got {entry!r}"
            )
        # Per-job radius override falls through to the defaults value.
        # int() coerces in case TOML loads it as something exotic.
        radius = int(entry.get("radius", default_radius))

        for category in categories:
            if not isinstance(category, str):
                raise LeadScoutError(
                    f"`categories` must be a list of strings; got {category!r}"
                )
            if category not in KNOWN_BUSINESS_TYPES and category not in warned_unknowns:
                logger.warning(
                    "Plan category %r is not in KNOWN_BUSINESS_TYPES. "
                    "Will run anyway. If results are empty, check the "
                    "Places 'Table A' types list.",
                    category,
                )
                warned_unknowns.add(category)
            jobs.append(JobSpec(location=location, category=category, radius=radius))

    logger.info("Loaded campaign plan: %d job(s) from %s", len(jobs), path)
    return jobs, defaults


# ---------------------------------------------------------------------------
# Daily slice: skip already-fresh (location, category) pairs.
# ---------------------------------------------------------------------------


def _is_job_fresh(
    job: JobSpec,
    data_dir: Path,
    cutoff: datetime,
) -> bool:
    """Return True if the on-disk JSON for `job.location` already
    contains businesses matching `job.category` whose `last_scanned`
    is at or after `cutoff` for ALL of them.

    We use "all" rather than "any" so that adding a new category to
    a previously-scanned location triggers a re-search (the new
    category's businesses won't be in the file yet).

    No matching businesses on disk = not fresh (run the job). One
    or more matches but any of them is stale = not fresh (run to
    refresh).
    """
    path = data_path_for_location(data_dir, job.location)
    if not path.exists():
        return False
    try:
        existing = load_data(path)
    except Exception as e:  # noqa: BLE001
        # Defensive: a corrupt file shouldn't make us skip the job
        # (we'd rather re-run and overwrite than leave stale data).
        logger.warning("Could not read %s for freshness check: %s", path, e)
        return False

    # Filter to records matching this job's category. The category
    # we'd recover from search is `business_type`; chain-blocked or
    # dead-listing entries never get persisted in the first place.
    matching = [b for b in existing if b.business_type == job.category]
    if not matching:
        return False

    # Every match must be fresh. last_scanned is None on records
    # that predate feature 02's timestamping; treat None as stale
    # so old data triggers a refresh.
    return all(
        b.last_scanned is not None and b.last_scanned >= cutoff
        for b in matching
    )


def select_todays_jobs(
    jobs: list[JobSpec],
    data_dir: Path,
    *,
    refresh_days: int = CAMPAIGN_REFRESH_DAYS,
) -> tuple[list[JobSpec], list[JobSpec]]:
    """Partition jobs into (todo, skipped_fresh) by freshness.

    Args:
        jobs: All jobs from `load_plan`.
        data_dir: Where per-location JSONs live.
        refresh_days: Treat a job as fresh if every matching
            business has a `last_scanned` newer than now - this.

    Returns:
        (todo, skipped_fresh). Order in each list preserves the
        input ordering (campaign processes jobs in plan order).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=refresh_days)
    todo: list[JobSpec] = []
    skipped: list[JobSpec] = []
    for job in jobs:
        if _is_job_fresh(job, data_dir, cutoff):
            logger.info(
                "Skipping %s/%s: fresh within %d days.",
                job.location, job.category, refresh_days,
            )
            skipped.append(job)
        else:
            todo.append(job)
    return todo, skipped


# ---------------------------------------------------------------------------
# Top-level orchestrator.
# ---------------------------------------------------------------------------


def run_campaign(
    plan_path: Path,
    data_dir: Path,
    *,
    places_key: str,
    cs_key: str | None,
    cs_cx: str | None,
    psi_key: str | None,
    max_jobs: int | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> CampaignSummary:
    """Run today's slice of the campaign.

    Steps:
    1. Load + validate plan.
    2. Select today's jobs (skip already-fresh combos).
    3. For each job, check Places quota. If below safe limit, run
       `pipeline.run_pipeline` for that one job. Track per-job
       counts of dead/chain filtered.
    4. Halt cleanly when quota hits safe limit; return summary.
    5. On `dry_run`, do steps 1-2 and report what would run, but
       skip step 3 entirely (no API calls, no quota consumption).

    Args:
        plan_path: TOML plan file.
        data_dir: Where per-location JSONs and the .places_quota.json
            tracker file live.
        places_key, cs_key, cs_cx, psi_key: Same env-derived API
            credentials run_pipeline takes. Caller (the CLI handler)
            reads them from env and passes them through.
        max_jobs: Hard cap on jobs run this invocation. None = run
            until quota or queue exhausts. Useful for testing.
        dry_run: Compute the plan + today's queue but don't execute.
        force: Pass-through to `run_pipeline` to override the audit
            7-day freshness window (typically unused in campaign mode
            because freshness already gates job selection).

    Returns:
        CampaignSummary with counts, halt reason, and remaining queue.
    """
    summary = CampaignSummary()

    # --- Plan loading ---
    jobs, defaults = load_plan(plan_path)
    summary.jobs_total = len(jobs)

    # Defaults dict: per-key fallback to module constants. Letting
    # the plan override these (e.g., min_review_count = 5 for a
    # stricter campaign) is the point of [defaults].
    min_review_count = int(
        defaults.get("min_review_count", CAMPAIGN_MIN_REVIEW_COUNT)
    )
    refresh_days = int(defaults.get("refresh_days", CAMPAIGN_REFRESH_DAYS))

    # --- Daily slice ---
    todo, skipped = select_todays_jobs(
        jobs, data_dir, refresh_days=refresh_days,
    )
    summary.jobs_skipped_fresh = len(skipped)

    # --- Places quota tracker ---
    # Initialized even on dry runs so we can report `remaining` to
    # the user. consume() is gated on `dry_run` below.
    quota = PlacesQuotaTracker(
        data_dir,
        safe_limit=PLACES_SAFE_LIMIT,
        warn_threshold=PLACES_WARN_THRESHOLD,
    )
    summary.places_quota_safe_limit = quota.safe_limit

    if dry_run:
        # Surface the queue and quota state, then bail without doing
        # any work. The CLI handler prints a per-job dry-run line.
        summary.jobs_remaining = len(todo)
        summary.places_quota_used = quota.count
        summary.queued_jobs = [(j.location, j.category) for j in todo]
        summary.halted_reason = "dry-run (no API calls issued)"
        logger.info(
            "Dry run: %d job(s) would execute today; %d already fresh.",
            len(todo), len(skipped),
        )
        return summary

    # --- Job execution loop ---
    # Each iteration: peek at remaining quota, run one job (which may
    # consume any number of Places calls — typically just 1 per job
    # since Nearby Search returns ≤20 results in one shot, plus 1 for
    # geocoding).
    for index, job in enumerate(todo):
        if max_jobs is not None and summary.jobs_run >= max_jobs:
            summary.halted_reason = (
                f"--max-jobs cap of {max_jobs} reached"
            )
            break
        # Check quota before consuming. We need at minimum 2 Places
        # calls per job (1 geocode + 1 nearby), so refuse to start
        # a job without that headroom — better to defer to tomorrow
        # than to halt a job mid-flight after the geocoding call.
        if quota.remaining < 2:
            summary.halted_reason = (
                f"Places quota at safe limit ({quota.count}/"
                f"{quota.safe_limit}); resumes at UTC midnight"
            )
            break

        logger.info(
            "[campaign] job %d/%d: %s / %s (radius=%dm, "
            "places_remaining=%d)",
            index + 1, len(todo), job.location, job.category,
            job.radius, quota.remaining,
        )
        try:
            # Snapshot disk count for this location before running so
            # we can compute how many businesses got filtered (kept
            # vs. on-disk-after) for the summary.
            path = data_path_for_location(data_dir, job.location)
            before_count = len(load_data(path)) if path.exists() else 0
            run_pipeline(
                job.location, job.radius, [job.category], data_dir,
                places_key=places_key, cs_key=cs_key, cs_cx=cs_cx,
                psi_key=psi_key,
                force=force,
                min_review_count=min_review_count,
                blocked_chain_names=BLOCKED_CHAIN_NAMES,
                places_quota=quota,
                # Suppress the per-stage echo lines for campaign
                # output — the per-job log line above is enough.
                echo=False,
            )
            after_count = len(load_data(path)) if path.exists() else 0
            # Net new businesses persisted. Drops (dead/chain) don't
            # count toward this — they never make it to disk. We can't
            # break out exactly which were dead vs chain at this layer
            # without plumbing more state, so the summary tracks the
            # combined "filtered before save" delta as a single number.
            net_new = max(0, after_count - before_count)
            logger.info(
                "[campaign] job %d/%d done: net new businesses=%d, "
                "places_used=%d/%d.",
                index + 1, len(todo), net_new,
                quota.count, quota.safe_limit,
            )
            summary.jobs_run += 1
        except APIError as e:
            # If Places quota throws mid-job we stop the campaign;
            # any other APIError (auth, transient) is logged but the
            # campaign continues so one bad location doesn't kill
            # the whole day's queue.
            if "Places daily safe limit" in str(e):
                summary.halted_reason = str(e)
                break
            logger.warning(
                "[campaign] job %d/%d failed (%s); continuing.",
                index + 1, len(todo), e,
            )
            continue
        except LeadScoutError as e:
            logger.warning(
                "[campaign] job %d/%d skipped due to error: %s",
                index + 1, len(todo), e,
            )
            continue

    # --- Final summary fields ---
    summary.places_quota_used = quota.count
    # Remaining jobs = whatever's left in `todo` past the last index
    # we successfully executed (not counting skipped_fresh, those are
    # in their own bucket).
    remaining_slice = todo[summary.jobs_run:]
    summary.jobs_remaining = len(remaining_slice)
    summary.queued_jobs = [(j.location, j.category) for j in remaining_slice]
    if summary.halted_reason is None and summary.jobs_remaining == 0:
        summary.halted_reason = "all jobs complete"

    return summary
