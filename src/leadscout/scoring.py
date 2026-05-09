"""Lead scoring (feature 05): the final pipeline stage.

Takes businesses enriched by features 02-04 (search-discovered URLs,
domain-classified, audited where possible) and assigns each one a
`Lead` (tier + numeric score 0-100 + plain-English reason list).

Tier assignment, numeric score, and the reason list all consume the
same data:
- url_classification (set by feature 02 / refined by feature 03)
- audit (populated by feature 04 for official_site URLs)
- rating (set by feature 02 from Google Places)

All thresholds and weights are config constants — `scoring.py` is
pure logic, no magic numbers — so the first real scan can inform
threshold tuning without code changes.

Mirrors the pattern from prior pipeline stages:
`fn(businesses, ...) -> list[Business]`, mutating in place.
"""

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from leadscout.config import (
    LEAD_TIER_BASE_SCORES,
    SCORE_BROKEN_ASSETS_BONUS,
    SCORE_MAX,
    SCORE_NO_PRESENCE_BONUS,
    SCORE_PER_DOM_FAILURE,
    SCORE_PERF_BAD_BONUS,
    SCORE_PERF_BAD_THRESHOLD,
    SCORE_RATING_TIER_1_BONUS,
    SCORE_RATING_TIER_2_BONUS,
    SCORE_SLOW_LOAD_BONUS,
    SCORE_SLOW_LOAD_THRESHOLD_S,
    SCORING_DOM_FAILURE_TIER_THRESHOLD,
    SCORING_DOM_FIELDS,
    SCORING_LIGHTHOUSE_FAIL_THRESHOLD,
)
from leadscout.models import (
    Audit,
    Business,
    Lead,
    LeadTier,
    UrlClassification,
)
from leadscout.storage import load_data

logger = logging.getLogger(__name__)


# Columns for CSV export. Order matters: this is what the reader sees.
CSV_COLUMNS = (
    "rank",
    "name",
    "score",
    "tier",
    "address",
    "phone",
    "website",
    "rating",
    "reasons",
)


# ---------------------------------------------------------------------------
# Top-level orchestration: score_leads.
# ---------------------------------------------------------------------------


def score_leads(businesses: list[Business]) -> list[Business]:
    """Score every business; mutate each `Business.lead` in place.

    Mirrors discover_urls / audit_websites: takes a Business list,
    populates `Lead` on each, returns the same list for chainability.

    Spec literal had `score_leads(businesses, audits: dict)` but feature
    04 already attaches audits to `Business.audit`; passing a separate
    dict would duplicate state and break the established pipeline pattern.
    """
    for biz in businesses:
        tier = _assign_tier(biz)
        score = _compute_score(biz, tier)
        reasons = _build_reasons(biz)
        biz.lead = Lead(tier=tier, score=score, reasons=reasons)

    # Summary log: tier distribution + top scores. Useful for spotting
    # whether the scan turned up real leads or mostly skip-tier noise.
    counts: dict[str, int] = {}
    for b in businesses:
        if b.lead is None:
            continue
        counts[b.lead.tier.value] = counts.get(b.lead.tier.value, 0) + 1
    logger.info("Scored %d businesses by tier: %s", len(businesses), counts)

    return businesses


def rank_leads(businesses: list[Business]) -> list[Business]:
    """Return businesses sorted descending by lead.score.

    Skip-tier entries stay in the list (still scored 0); callers can
    filter as needed. Sort is stable so equal-score entries retain
    their input ordering.
    """
    # Treat businesses without a lead as score 0 so they don't crash
    # the comparator; in practice score_leads should always populate.
    return sorted(
        businesses,
        key=lambda b: b.lead.score if b.lead else 0,
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Tier assignment.
# ---------------------------------------------------------------------------


def _assign_tier(business: Business) -> LeadTier:
    """Classify a business into one of the four lead tiers.

    Order of evaluation matches the spec:
    1. no_website: classification != official_site (no real owned site).
    2. failing_audit: official site, BUT bad Lighthouse OR 3+ DOM gaps.
    3. missing_features: official site, basic Lighthouse passes, 1-2 DOM gaps.
    4. skip: official site, no Lighthouse failures, no DOM gaps.
    """
    if business.url_classification != UrlClassification.OFFICIAL_SITE:
        return LeadTier.NO_WEBSITE

    audit = business.audit
    # Official-site business with no audit at all: we can't judge it.
    # Treat as SKIP rather than guessing; --force on a re-audit will
    # fill in real data later.
    if audit is None:
        return LeadTier.SKIP

    if _is_failing_audit(audit):
        return LeadTier.FAILING_AUDIT

    failures = _count_dom_failures(audit)
    if failures >= 1:
        return LeadTier.MISSING_FEATURES
    return LeadTier.SKIP


def _is_failing_audit(audit: Audit) -> bool:
    """Decide whether an audit is bad enough for FAILING_AUDIT tier.

    Three independent triggers, any one fires:
    - mobile performance below threshold,
    - mobile accessibility below threshold,
    - 3+ failed DOM checks across SCORING_DOM_FIELDS.
    """
    mobile = audit.lighthouse_mobile or {}
    perf = mobile.get("performance")
    a11y = mobile.get("accessibility")
    if perf is not None and perf < SCORING_LIGHTHOUSE_FAIL_THRESHOLD:
        return True
    if a11y is not None and a11y < SCORING_LIGHTHOUSE_FAIL_THRESHOLD:
        return True
    if _count_dom_failures(audit) >= SCORING_DOM_FAILURE_TIER_THRESHOLD:
        return True
    return False


def _count_dom_failures(audit: Audit) -> int:
    """Count missing customer-facing DOM checks (out of SCORING_DOM_FIELDS).

    `getattr` lookup keeps SCORING_DOM_FIELDS as the single source of
    truth: change the tuple in config and both the tier-failure count
    and the per-failure score modifier update together.
    """
    return sum(1 for f in SCORING_DOM_FIELDS if not getattr(audit, f))


# ---------------------------------------------------------------------------
# Score computation.
# ---------------------------------------------------------------------------


def _compute_score(business: Business, tier: LeadTier) -> int:
    """Compute the 0-100 lead score for a business.

    Sum of:
    - tier base (per LEAD_TIER_BASE_SCORES)
    - additive modifiers (no-presence, rating, DOM gaps, perf, slow load,
      broken assets)
    capped at SCORE_MAX. Skip tier always returns 0 regardless of
    modifiers (it's not a lead, no need to rank it).
    """
    if tier == LeadTier.SKIP:
        return 0

    score = LEAD_TIER_BASE_SCORES[tier.value]

    # No web presence at all (not even social/directory). Distinct
    # from "no_website" tier which also covers social-only businesses.
    if business.url_classification == UrlClassification.NONE:
        score += SCORE_NO_PRESENCE_BONUS

    # Rating bonuses are stacked: 4.5+ businesses get both tier-1 (+5)
    # and tier-2 (+5) for a total of +10. Lower-rated businesses get
    # less weight because they're either struggling or rapidly going
    # under, neither making a strong pitch.
    rating = business.rating
    if rating is not None:
        if rating >= 4.0:
            score += SCORE_RATING_TIER_1_BONUS
        if rating >= 4.5:
            score += SCORE_RATING_TIER_2_BONUS

    audit = business.audit
    if audit is not None:
        # Per-failure DOM modifier. Same field set as the tier-threshold
        # check so a business with 4 DOM gaps (failing_audit tier) also
        # gets +8 on its score from the modifier.
        score += _count_dom_failures(audit) * SCORE_PER_DOM_FAILURE

        # Lighthouse mobile perf below SCORE_PERF_BAD_THRESHOLD (30):
        # a separate, harder threshold than the tier-threshold (50).
        # A site at perf=25 is failing both thresholds and gets the
        # tier promotion AND this score bonus.
        perf = (audit.lighthouse_mobile or {}).get("performance")
        if perf is not None and perf < SCORE_PERF_BAD_THRESHOLD:
            score += SCORE_PERF_BAD_BONUS

        load_time = audit.load_time_seconds
        if (
            load_time is not None
            and load_time > SCORE_SLOW_LOAD_THRESHOLD_S
        ):
            score += SCORE_SLOW_LOAD_BONUS

        if audit.broken_assets:
            score += SCORE_BROKEN_ASSETS_BONUS

    return min(score, SCORE_MAX)


# ---------------------------------------------------------------------------
# Reason builder.
# ---------------------------------------------------------------------------


def _build_reasons(business: Business) -> list[str]:
    """Build the plain-English reason list for a Lead.

    Order matters (it's what Cole reads in the ranked summary):
    1. The single most important headline reason (no website at all,
       only social, only directory).
    2. The audit-derived deficiencies (already curated by feature 04's
       _compute_deficiencies — we reuse that output for cohesion
       rather than re-deriving with slightly different wording).
    3. Slow-load callout, which feature 04 doesn't include in
       audit.deficiencies because it's a scoring concern, not a
       fundamental site failure.
    """
    reasons: list[str] = []

    cls = business.url_classification
    if cls == UrlClassification.NONE:
        reasons.append("No website found")
    elif cls == UrlClassification.SOCIAL_MEDIA:
        reasons.append("Only social media presence")
    elif cls == UrlClassification.DIRECTORY_LISTING:
        reasons.append("Only directory listing")

    audit = business.audit
    if audit is not None:
        # Feature 04 built audit.deficiencies as the curated list for
        # exactly this consumption. Append rather than re-derive.
        reasons.extend(audit.deficiencies)

        # Slow load isn't in audit.deficiencies (intentionally scoped
        # there to "site is broken" issues). Add the scoring-specific
        # reason here.
        load_time = audit.load_time_seconds
        if (
            load_time is not None
            and load_time > SCORE_SLOW_LOAD_THRESHOLD_S
        ):
            reasons.append(f"Slow load time: {load_time}s")

    return reasons


# ---------------------------------------------------------------------------
# CSV export.
# ---------------------------------------------------------------------------


def export_to_csv(businesses: list[Business], path: Path) -> None:
    """Write ranked leads to a CSV file at `path`. Excludes skip-tier rows.

    Uses stdlib csv (no pandas, per spec). Sorts descending by score
    inside this function so callers don't have to remember to rank
    first. Reasons are joined with semicolons because commas would
    collide with CSV's separator unless quoted (and unquoted is easier
    to scan in a spreadsheet).
    """
    # Filter and rank. Skip-tier entries stay in the JSON for record
    # purposes but don't belong in the leads CSV.
    leads = [
        b
        for b in businesses
        if b.lead is not None and b.lead.tier != LeadTier.SKIP
    ]
    leads.sort(key=lambda b: b.lead.score, reverse=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" required on Windows so csv doesn't double-write \r\n.
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for rank, biz in enumerate(leads, start=1):
            assert biz.lead is not None  # filtered above
            writer.writerow(
                {
                    "rank": rank,
                    "name": biz.name,
                    "score": biz.lead.score,
                    "tier": biz.lead.tier.value,
                    "address": biz.address,
                    "phone": biz.phone,
                    "website": biz.website,
                    "rating": biz.rating if biz.rating is not None else "",
                    "reasons": "; ".join(biz.lead.reasons),
                }
            )
    logger.info("Exported %d ranked leads to %s", len(leads), path)


def csv_path_for_data_file(data_file: Path) -> Path:
    """Derive the CSV output path from a data file path.

    `data/santa_rosa_beach_fl.json` -> `data/leads_santa_rosa_beach_fl_2026-05-03.csv`.
    Uses today's UTC date so the same scan run twice in a day produces
    one file per day (spec's `{date}` placeholder is intentionally
    coarse).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    return data_file.parent / f"leads_{data_file.stem}_{today}.csv"


# ---------------------------------------------------------------------------
# Markdown export.
# ---------------------------------------------------------------------------


# Tier display order in the summary table. Matches the human-meaningful
# priority: best leads first, skip last.
_TIER_DISPLAY_ORDER = (
    LeadTier.NO_WEBSITE,
    LeadTier.FAILING_AUDIT,
    LeadTier.MISSING_FEATURES,
    LeadTier.SKIP,
)


def export_to_markdown(
    businesses: list[Business],
    path: Path,
    *,
    location_label: str | None = None,
) -> None:
    """Write ranked leads to a human-readable markdown file at `path`.

    Always-on for the score / run CLI commands (per the request: "format
    each run's output into an easy-to-read markdown file"). CSV remains
    opt-in for spreadsheet import; markdown is the report you actually
    read when deciding who to contact.

    Format: a short header (location + date + totals), a tier-count
    summary table, and a per-business detail section ordered by score
    descending. Skip-tier entries are excluded (no lead value), matching
    CSV behavior; the tier summary still counts them so the reader sees
    the full scan picture.

    location_label is the friendly title shown at the top. Pass the
    user's original location string when calling from `run`; falls back
    to the data file's stem (slug form) when called from `score`.
    """
    leads = [
        b
        for b in businesses
        if b.lead is not None and b.lead.tier != LeadTier.SKIP
    ]
    leads.sort(key=lambda b: b.lead.score, reverse=True)

    # Tier counts including skip so the summary line tells the full story.
    tier_counts: dict[str, int] = {}
    for b in businesses:
        if b.lead is None:
            continue
        tier_counts[b.lead.tier.value] = (
            tier_counts.get(b.lead.tier.value, 0) + 1
        )

    today = datetime.now(timezone.utc).date().isoformat()
    title = location_label or path.stem

    lines: list[str] = []
    # Header. Two trailing spaces on metadata lines force markdown line
    # breaks so each "**Label:**" sits on its own line in rendered view.
    lines.append(f"# LeadScout: {title}")
    lines.append("")
    lines.append(f"**Scan date:** {today} (UTC)  ")
    lines.append(f"**Total businesses scanned:** {len(businesses)}  ")
    lines.append(f"**Active leads (above skip tier):** {len(leads)}")
    lines.append("")

    # Tier summary table. Iterate in display order, defaulting absent
    # tiers to 0 so the table is consistent even on partial scans.
    lines.append("| Tier | Count |")
    lines.append("| --- | --- |")
    for tier in _TIER_DISPLAY_ORDER:
        lines.append(f"| {tier.value} | {tier_counts.get(tier.value, 0)} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Active leads")
    lines.append("")

    if not leads:
        lines.append("_No leads above skip tier._")
    else:
        for rank, biz in enumerate(leads, start=1):
            lead = biz.lead
            assert lead is not None  # filtered above
            # Heading combines rank, name, score, tier so a reader can
            # scan the document via the table-of-contents in any
            # markdown viewer.
            lines.append(
                f"### {rank}. {biz.name} — score {lead.score} — `{lead.tier.value}`"
            )
            lines.append("")
            # Use em-dash placeholder for empty fields so missing data is
            # visually distinct from "we found this and it's empty".
            rating_display = (
                f"{biz.rating}" if biz.rating is not None else "—"
            )
            lines.append(f"- **Address:** {biz.address or '—'}")
            lines.append(f"- **Phone:** {biz.phone or '—'}")
            lines.append(f"- **Website:** {biz.website or '—'}")
            lines.append(f"- **Rating:** {rating_display}")
            lines.append("- **Why it's a lead:**")
            for reason in lead.reasons:
                lines.append(f"  - {reason}")
            lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    # Trailing newline for clean POSIX-style file end.
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Exported %d ranked leads to %s", len(leads), path)


def markdown_path_for_data_file(data_file: Path) -> Path:
    """Derive the markdown report path from a data file path.

    `data/santa_rosa_beach_fl.json` -> `data/leads_santa_rosa_beach_fl_2026-05-03.md`.
    Same naming pattern as `csv_path_for_data_file`; the two outputs
    sit next to each other in the data dir for the same scan.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    return data_file.parent / f"leads_{data_file.stem}_{today}.md"


# ---------------------------------------------------------------------------
# Cross-location aggregation (feature 06: campaign mode).
# ---------------------------------------------------------------------------


# Tier ordering used for the report's `--min-tier` filter. Indices ascend
# from "best lead" (no_website) to "not a lead" (skip). A min-tier of
# `missing_features` includes everything strictly better, i.e. tiers at
# index <= MISSING_FEATURES's index.
_TIER_QUALITY_ORDER = (
    LeadTier.NO_WEBSITE,
    LeadTier.FAILING_AUDIT,
    LeadTier.MISSING_FEATURES,
    LeadTier.SKIP,
)


def aggregate_leads(data_dir: Path) -> list[Business]:
    """Load every per-location JSON in `data_dir` into a single list.

    Used by the `report` CLI command (feature 06) to roll up a multi-day
    campaign's results across many locations. No deduplication: a chain
    franchise in two cities has two distinct `place_id`s and represents
    two distinct sales prospects, so we keep both.

    Files starting with a dot (e.g. `.places_quota.json`,
    `.custom_search_quota.json`) are skipped because they're internal
    state, not business records. We filter on the leading dot in code
    rather than relying on the OS-shell glob behavior so this works
    identically across platforms (PowerShell on Windows does NOT skip
    dotfiles by default the way bash does).

    Returns an empty list if `data_dir` doesn't exist yet (first-run
    convenience matching `load_data`'s missing-file behavior).
    """
    # Defensive early return for a missing data dir. Path.glob on a
    # non-existent path silently yields nothing, so this is mostly for
    # the explicit log line; without it, an empty result here is
    # ambiguous between "no scans yet" and "data_dir typo'd."
    if not data_dir.exists():
        logger.warning("Data dir %s does not exist; nothing to aggregate.", data_dir)
        return []

    combined: list[Business] = []
    files_loaded = 0
    # Sorted for stable output; `glob` makes no ordering guarantees and
    # the report's tier-summary table reads better when the same files
    # are processed in the same order across invocations.
    for path in sorted(data_dir.glob("*.json")):
        if path.name.startswith("."):
            # Hidden state file (e.g., quota tracker). Skip.
            continue
        try:
            businesses = load_data(path)
        except Exception as e:  # noqa: BLE001
            # One bad file shouldn't fail the whole report; surface the
            # error and continue. load_data already raises StorageError
            # on parse failures, so the message is already user-friendly.
            logger.warning("Skipping %s: %s", path, e)
            continue
        files_loaded += 1
        combined.extend(businesses)

    logger.info(
        "Aggregated %d businesses from %d location file(s) under %s.",
        len(combined), files_loaded, data_dir,
    )
    return combined


def _location_label_from_address(address: str) -> str:
    """Pull a 'City, ST' label out of a Google `formattedAddress`.

    Google's formatted addresses are predictable: "100 Beach Rd, Santa
    Rosa Beach, FL 32459, USA". The penultimate comma-segment usually
    holds the city; the segment after that holds "ST ZIP". Returning
    "City, ST" gives the report a readable per-location grouping
    without needing a separate field on Business.

    Fall back to the trimmed full address (or "—") on anything we
    can't parse — defensive default, never raise.
    """
    if not address:
        return "—"
    # Split on comma, then strip whitespace from each piece. Filter
    # out empties so trailing commas don't leave a "" in the list.
    parts = [p.strip() for p in address.split(",") if p.strip()]
    # Typical shape: ["<street>", "<city>", "<ST ZIP>", "<country>"].
    # Take the last 3 (drop street/country if present), then pull
    # city + first token of "ST ZIP". Anything shorter falls through.
    if len(parts) >= 3:
        # Country comes last; city is second from end if there are 4+
        # parts, third from end otherwise.
        country_present = parts[-1].lower() in {"usa", "united states"}
        if country_present and len(parts) >= 4:
            city = parts[-3]
            state_zip = parts[-2]
        else:
            city = parts[-2]
            state_zip = parts[-1]
        # First token of "ST ZIP" is the state abbreviation.
        state_token = state_zip.split()[0] if state_zip.split() else ""
        if city and state_token:
            return f"{city}, {state_token}"
    # Fallback: just the original address, useful for non-US results.
    return address


def export_campaign_markdown(
    businesses: list[Business],
    path: Path,
    *,
    top_n: int = 50,
    min_tier: LeadTier = LeadTier.MISSING_FEATURES,
) -> None:
    """Write a cross-location ranked markdown to `path`.

    Used by the `report` CLI command. Differs from `export_to_markdown`
    in three ways:
    - Adds a per-location summary table at the top (row per City, ST
      derived from address, with active-lead count).
    - Includes a `Location` line on each business detail block so the
      reader can group/sort visually.
    - `top_n` caps the detail section size; everything below the cut
      is still counted in the per-location table but not rendered
      individually (a 7-day campaign across many categories can
      produce hundreds of leads — surface only the most actionable).

    `min_tier` is inclusive: passing `MISSING_FEATURES` includes
    `no_website` and `failing_audit` plus `missing_features`, but
    excludes `skip`. The default mirrors `export_to_markdown`.
    """
    # Tier index lookup: position in the quality order tuple. A tier
    # qualifies if its index is <= the min-tier's index. Built once
    # so the per-business filter is a dict lookup, not a list search.
    tier_rank = {t: i for i, t in enumerate(_TIER_QUALITY_ORDER)}
    cutoff = tier_rank[min_tier]

    # Filter to qualifying leads and sort by score. Skip-tier always
    # falls out (its index is past every reasonable cutoff). Sort is
    # stable so businesses with equal scores keep input order.
    qualifying = [
        b
        for b in businesses
        if b.lead is not None and tier_rank.get(b.lead.tier, 999) <= cutoff
    ]
    qualifying.sort(key=lambda b: b.lead.score, reverse=True)

    # Per-location bucket counts. Keys are "City, ST" labels; values
    # are total counts in that location (qualifying only — skip-tier
    # is intentionally excluded from this rollup).
    location_counts: dict[str, int] = {}
    for biz in qualifying:
        label = _location_label_from_address(biz.address)
        location_counts[label] = location_counts.get(label, 0) + 1

    # Slice for detail section. The table totals above stay full; only
    # the per-business detail blocks honor top_n.
    rendered = qualifying[:top_n]

    today = datetime.now(timezone.utc).date().isoformat()
    lines: list[str] = []
    # Header. Two trailing spaces on metadata lines force markdown
    # line breaks the same way `export_to_markdown` does.
    lines.append("# LeadScout Campaign Report")
    lines.append("")
    lines.append(f"**Report date:** {today} (UTC)  ")
    lines.append(f"**Total businesses scanned:** {len(businesses)}  ")
    lines.append(
        f"**Active leads (tier ≥ `{min_tier.value}`):** {len(qualifying)}  "
    )
    lines.append(f"**Locations covered:** {len(location_counts)}")
    lines.append("")

    # Per-location summary. Sorted by count desc so the most productive
    # locations sit at the top. Empty when no qualifying leads exist
    # (avoids printing an empty table).
    if location_counts:
        lines.append("## Leads by location")
        lines.append("")
        lines.append("| Location | Active leads |")
        lines.append("| --- | --- |")
        for label, count in sorted(
            location_counts.items(), key=lambda kv: kv[1], reverse=True
        ):
            lines.append(f"| {label} | {count} |")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Detail section. Same format as the per-location markdown but with
    # an extra Location line; rendering only the top_n rows so the
    # output stays scannable for a multi-day campaign.
    if not rendered:
        lines.append(
            f"_No leads at tier `{min_tier.value}` or better across "
            "the campaign._"
        )
    else:
        cap_note = "" if len(rendered) == len(qualifying) else (
            f" (showing top {len(rendered)} of {len(qualifying)})"
        )
        lines.append(f"## Top leads{cap_note}")
        lines.append("")
        for rank, biz in enumerate(rendered, start=1):
            lead = biz.lead
            assert lead is not None  # filtered above
            location = _location_label_from_address(biz.address)
            lines.append(
                f"### {rank}. {biz.name} — score {lead.score} — "
                f"`{lead.tier.value}`"
            )
            lines.append("")
            rating_display = (
                f"{biz.rating}" if biz.rating is not None else "—"
            )
            lines.append(f"- **Location:** {location}")
            lines.append(f"- **Address:** {biz.address or '—'}")
            lines.append(f"- **Phone:** {biz.phone or '—'}")
            lines.append(f"- **Website:** {biz.website or '—'}")
            lines.append(f"- **Rating:** {rating_display}")
            lines.append("- **Why it's a lead:**")
            for reason in lead.reasons:
                lines.append(f"  - {reason}")
            lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(
        "Wrote campaign markdown (%d leads, %d locations) to %s",
        len(qualifying), len(location_counts), path,
    )
