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
