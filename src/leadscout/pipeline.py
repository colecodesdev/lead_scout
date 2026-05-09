"""Single-location pipeline orchestration (feature 06).

The body that used to live inline in `cli.run` (search -> filter ->
discover -> audit -> score -> save) is extracted here so two callers
can reuse it without duplication:

1. The existing `leadscout run` CLI command — passes one location +
   one or more categories, writes the same per-location markdown it
   always has.
2. The new `leadscout campaign` command — calls this once per
   `(location, category)` job in a multi-day plan.

Behavior is preserved bit-for-bit relative to the prior `cli.run`
body. The two changes are additive and gated:
- A `filter_live_businesses` pass is inserted right after search, with
  defaults (min_review_count=1) that drop only literal zero-review
  listings.
- A `PlacesQuotaTracker` may be threaded through to `search_places`
  so a long campaign can hard-stop before exceeding the daily Maps
  Platform budget. `run` callers pass `None` (no per-day cap on a
  single ad-hoc invocation).

Network IO and disk writes happen here. CLI handlers stay thin: env
vars in, formatted output out, errors translated to user-facing
messages.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import click

from leadscout.audit import audit_websites
from leadscout.discovery import discover_urls, reclassify_urls
from leadscout.exceptions import APIError
from leadscout.filtering import filter_live_businesses
from leadscout.models import Business
from leadscout.scoring import score_leads
from leadscout.search import search_places
from leadscout.storage import data_path_for_location, load_data, merge_business, save_data

# Avoid a runtime import cycle if places_quota ever pulls from this
# module. Annotation-only import is enough for the type hint.
if TYPE_CHECKING:
    from leadscout.places_quota import PlacesQuotaTracker

logger = logging.getLogger(__name__)


def run_pipeline(
    location: str,
    radius: int,
    categories: list[str],
    data_dir: Path,
    *,
    places_key: str,
    cs_key: str | None,
    cs_cx: str | None,
    psi_key: str | None,
    force: bool = False,
    min_review_count: int = 0,
    blocked_chain_names: frozenset[str] | None = None,
    places_quota: "PlacesQuotaTracker | None" = None,
    echo: bool = True,
) -> list[Business]:
    """Run the full pipeline for one location and one or more categories.

    Returns the merged-and-scored business list (post-save). The on-disk
    JSON for this location is updated as a side effect, so callers that
    only care about progress can ignore the return value.

    Args:
        location: Human-readable place string ("Santa Rosa Beach, FL").
        radius: Nearby Search radius in meters.
        categories: One or more Google Places "Table A" type strings.
        data_dir: Directory holding per-location JSON files.
        places_key: Required Google Places API key.
        cs_key, cs_cx: Custom Search credentials. When either is None,
            discovery soft-fails to local URL classification (matches
            the prior `cli.run` behavior — Custom Search isn't always
            available depending on GCP project age).
        psi_key: PageSpeed Insights key. May be the same key as Places
            (PSI accepts the Maps key as fallback). None disables PSI
            and runs only the Playwright DOM checks.
        force: Pass-through to `discover_urls` / `audit_websites` to
            re-run on records that would otherwise be skipped by the
            7-day freshness window.
        min_review_count: Drops businesses with fewer reviews than this
            BEFORE discovery (so dead listings don't burn quota).
            Default 0 disables the filter (single-location ad-hoc
            invocations of `leadscout run` keep every result). The
            campaign command always passes CAMPAIGN_MIN_REVIEW_COUNT.
        blocked_chain_names: Frozenset of normalized substring fragments
            for the chain-block filter. None disables chain filtering
            (single-location ad-hoc runs may want to keep all listings).
        places_quota: Optional tracker; when supplied, `search_places`
            increments it per request and raises APIError if the safe
            limit is reached.
        echo: When True, prints the same `[N/4] stage : ...` progress
            lines the existing `run` CLI command emits. The campaign
            command uses False to keep its job-level summary clean.

    Raises:
        APIError if any stage other than Custom Search hard-fails
        (Custom Search soft-fails to reclassify_urls, see below).
    """
    # Print helper. Local def keeps the `if echo:` guard out of every
    # call site without dragging click into the function signature.
    def say(msg: str) -> None:
        if echo:
            click.echo(msg)

    # Decide upfront whether Custom Search is available. None means yes
    # (we'll try); a non-None reason string means we'll skip discovery.
    skip_discover_reason: str | None = None
    if not cs_key or not cs_cx:
        skip_discover_reason = (
            "GOOGLE_CUSTOM_SEARCH_API_KEY or GOOGLE_CUSTOM_SEARCH_CX not set"
        )

    # Per-location JSON path. Slug logic centralized in storage.py so
    # `search`, `run`, and the new campaign all derive identical paths.
    path = data_path_for_location(data_dir, location)

    # --- Stage 1: search (Google Places) ---
    say(
        f"[1/4] search   : {location} (radius={radius}m, "
        f"types={list(categories)})"
    )
    found = search_places(
        location, radius, places_key,
        included_types=list(categories),
        quota=places_quota,
    )

    # --- Filter dead listings + blocked chains BEFORE discover ---
    # Inserted between search and merge so the filter applies to fresh
    # API results, not to historically-saved records (which may have
    # been kept on disk for valid reasons before the filter existed).
    kept, dropped = filter_live_businesses(
        found,
        min_review_count=min_review_count,
        blocked_chain_names=blocked_chain_names,
    )
    if dropped:
        say(f"        filtered: {len(dropped)} dead/chain dropped")

    # Merge into existing data if the location was scanned before.
    # Same place_id-keyed dict approach the prior cli.run body used.
    existing = load_data(path)
    by_id = {b.place_id: b for b in existing}
    for b in kept:
        if b.place_id in by_id:
            merge_business(by_id[b.place_id], b)
        else:
            by_id[b.place_id] = b
    businesses = list(by_id.values())
    save_data(path, businesses)
    say(f"        found  : {len(kept)} ({len(businesses)} total)")

    # --- Stage 2: discover (Custom Search + classify) ---
    if skip_discover_reason:
        # Env-var-driven skip path: never even try the network call.
        say(
            f"[2/4] discover : SKIPPED ({skip_discover_reason}); "
            "running classification only"
        )
        reclassify_urls(businesses)
    else:
        say("[2/4] discover : Custom Search + URL classification")
        try:
            discover_urls(
                businesses, cs_key, cs_cx,
                data_dir=data_dir, force=force,
            )
        except APIError as e:
            # Runtime denial (e.g., the "project does not have access"
            # 403 that affects new GCP projects). Fall back to the
            # local-only classification so the pipeline continues
            # rather than aborting before audit and score.
            say(f"        Custom Search unavailable: {e}")
            say("        Falling back to classification-only.")
            reclassify_urls(businesses)
    save_data(path, businesses)

    # --- Stage 3: audit (PSI + Playwright) ---
    say("[3/4] audit    : PageSpeed Insights + Playwright DOM")
    audit_websites(businesses, psi_key, force=force)
    save_data(path, businesses)

    # --- Stage 4: score ---
    say("[4/4] score    : tier + numeric ranking")
    score_leads(businesses)
    save_data(path, businesses)

    return businesses
