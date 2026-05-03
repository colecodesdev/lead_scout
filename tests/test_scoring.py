"""Tests for src/leadscout/scoring.py.

Strategy:
- Tier classification, score math, and reason building are pure functions.
  Tested directly with synthetic Business / Audit objects.
- Sort, CSV export tested directly against the file system (tmp_path).
- The spec's worked examples are encoded as named tests so any future
  threshold/weight tweaks immediately surface as test diffs.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

from leadscout.config import (
    SCORE_PER_DOM_FAILURE,
)
from leadscout.models import (
    Audit,
    Business,
    Lead,
    LeadTier,
    UrlClassification,
    UrlSource,
)
from leadscout.scoring import (
    CSV_COLUMNS,
    _assign_tier,
    _build_reasons,
    _compute_score,
    _count_dom_failures,
    _is_failing_audit,
    csv_path_for_data_file,
    export_to_csv,
    export_to_markdown,
    markdown_path_for_data_file,
    rank_leads,
    score_leads,
)


def _make_business(**overrides) -> Business:
    """Default to a fully-populated business; override per-test."""
    defaults = {
        "place_id": "abc",
        "name": "Test Cafe",
        "address": "100 Main St, Test, FL 32459, USA",
        "phone": "555-0000",
        "website": "https://test.example.com",
        "url_source": UrlSource.GOOGLE_PLACES,
        "url_classification": UrlClassification.OFFICIAL_SITE,
        "rating": 4.0,
    }
    defaults.update(overrides)
    return Business(**defaults)


def _passing_audit(**overrides) -> Audit:
    """Default to an audit with everything passing; override per-test."""
    defaults = {
        "lighthouse_mobile": {
            "performance": 90, "accessibility": 95,
            "seo": 90, "best_practices": 90,
        },
        "lighthouse_desktop": {
            "performance": 95, "accessibility": 95,
            "seo": 90, "best_practices": 90,
        },
        "has_menu": True,
        "has_hours": True,
        "has_contact_info": True,
        "has_mobile_viewport": True,
        "has_ssl": True,
        "has_online_ordering": True,
        "has_reservation": True,
        "load_time_seconds": 1.2,
        "broken_assets": [],
        "deficiencies": [],
        "audited_at": datetime(2026, 5, 3, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return Audit(**defaults)


# ---------------------------------------------------------------------------
# _count_dom_failures
# ---------------------------------------------------------------------------


class TestCountDomFailures:
    def test_zero_when_all_pass(self):
        assert _count_dom_failures(_passing_audit()) == 0

    def test_counts_each_missing_field(self):
        # Three missing customer-facing checks -> 3.
        audit = _passing_audit(
            has_menu=False, has_hours=False, has_ssl=False
        )
        assert _count_dom_failures(audit) == 3

    def test_mobile_viewport_not_counted(self):
        # SCORING_DOM_FIELDS intentionally excludes has_mobile_viewport
        # (developer concern, not customer-facing). A failing viewport
        # alone shouldn't bump the failure count.
        audit = _passing_audit(has_mobile_viewport=False)
        assert _count_dom_failures(audit) == 0


# ---------------------------------------------------------------------------
# _is_failing_audit
# ---------------------------------------------------------------------------


class TestIsFailingAudit:
    def test_low_performance_triggers(self):
        audit = _passing_audit(
            lighthouse_mobile={
                "performance": 25, "accessibility": 90,
                "seo": 90, "best_practices": 90,
            }
        )
        assert _is_failing_audit(audit) is True

    def test_low_accessibility_triggers(self):
        audit = _passing_audit(
            lighthouse_mobile={
                "performance": 80, "accessibility": 30,
                "seo": 90, "best_practices": 90,
            }
        )
        assert _is_failing_audit(audit) is True

    def test_three_dom_failures_triggers(self):
        audit = _passing_audit(
            has_menu=False, has_hours=False, has_ssl=False
        )
        assert _is_failing_audit(audit) is True

    def test_two_dom_failures_does_not_trigger(self):
        # With good Lighthouse and only 2 DOM gaps, this should be
        # "missing_features" tier, not "failing_audit".
        audit = _passing_audit(has_menu=False, has_hours=False)
        assert _is_failing_audit(audit) is False

    def test_passing_audit_does_not_trigger(self):
        assert _is_failing_audit(_passing_audit()) is False

    def test_none_lighthouse_scores_dont_trigger(self):
        # If PSI couldn't evaluate the page, don't penalize the business.
        audit = _passing_audit(
            lighthouse_mobile={
                "performance": None, "accessibility": None,
                "seo": None, "best_practices": None,
            }
        )
        assert _is_failing_audit(audit) is False


# ---------------------------------------------------------------------------
# _assign_tier
# ---------------------------------------------------------------------------


class TestAssignTier:
    def test_no_classification_is_no_website(self):
        biz = _make_business(url_classification=UrlClassification.NONE)
        assert _assign_tier(biz) == LeadTier.NO_WEBSITE

    def test_social_media_is_no_website(self):
        biz = _make_business(
            url_classification=UrlClassification.SOCIAL_MEDIA
        )
        assert _assign_tier(biz) == LeadTier.NO_WEBSITE

    def test_directory_listing_is_no_website(self):
        biz = _make_business(
            url_classification=UrlClassification.DIRECTORY_LISTING
        )
        assert _assign_tier(biz) == LeadTier.NO_WEBSITE

    def test_official_site_no_audit_is_skip(self):
        # We can't judge a site we haven't audited; treat as skip
        # rather than guessing.
        biz = _make_business(audit=None)
        assert _assign_tier(biz) == LeadTier.SKIP

    def test_official_site_failing_audit_tier(self):
        biz = _make_business(
            audit=_passing_audit(
                lighthouse_mobile={
                    "performance": 25, "accessibility": 90,
                    "seo": 90, "best_practices": 90,
                }
            )
        )
        assert _assign_tier(biz) == LeadTier.FAILING_AUDIT

    def test_official_site_one_dom_gap_is_missing_features(self):
        biz = _make_business(audit=_passing_audit(has_menu=False))
        assert _assign_tier(biz) == LeadTier.MISSING_FEATURES

    def test_official_site_two_dom_gaps_is_missing_features(self):
        biz = _make_business(
            audit=_passing_audit(has_menu=False, has_hours=False)
        )
        assert _assign_tier(biz) == LeadTier.MISSING_FEATURES

    def test_official_site_clean_audit_is_skip(self):
        biz = _make_business(audit=_passing_audit())
        assert _assign_tier(biz) == LeadTier.SKIP


# ---------------------------------------------------------------------------
# _compute_score (spec's worked examples + edges)
# ---------------------------------------------------------------------------


class TestComputeScore:
    def test_spec_example_1_no_website_high_rating_caps_at_100(self):
        # Spec test 1: no website, no social, rating 4.6. Score = 100.
        biz = _make_business(
            url_classification=UrlClassification.NONE,
            website="",
            rating=4.6,
            audit=None,
        )
        # Base 80 + no-presence 10 + rating tier1 5 + rating tier2 5 = 100.
        assert _compute_score(biz, LeadTier.NO_WEBSITE) == 100

    def test_spec_example_2_failing_audit_score_71(self):
        # Spec test 2: official site, mobile perf 25, no menu/hours/SSL.
        # Base 60 + 3 missing DOM (6) + perf<30 (5) = 71.
        biz = _make_business(
            rating=None,
            audit=_passing_audit(
                lighthouse_mobile={
                    "performance": 25, "accessibility": 90,
                    "seo": 90, "best_practices": 90,
                },
                has_menu=False, has_hours=False, has_ssl=False,
            ),
        )
        assert _compute_score(biz, LeadTier.FAILING_AUDIT) == 71

    def test_spec_example_3_skip_returns_zero(self):
        # Spec test 3: official site, all audits pass. Score = 0.
        biz = _make_business(audit=_passing_audit())
        assert _compute_score(biz, LeadTier.SKIP) == 0

    def test_no_presence_bonus_only_for_classification_none(self):
        # Social-media or directory businesses get the no_website tier
        # but NOT the +10 no-presence bonus (they at least have *some*
        # internet presence).
        biz = _make_business(
            url_classification=UrlClassification.SOCIAL_MEDIA,
            audit=None, rating=None,
        )
        # Base 80, no rating, no audit. No no-presence bonus.
        assert _compute_score(biz, LeadTier.NO_WEBSITE) == 80

    def test_rating_tier_1_only(self):
        # Rating 4.0: tier1 only (+5), no tier2.
        biz = _make_business(rating=4.0, audit=None)
        assert _compute_score(biz, LeadTier.NO_WEBSITE) == 85

    def test_rating_below_threshold_no_bonus(self):
        biz = _make_business(rating=3.9, audit=None)
        assert _compute_score(biz, LeadTier.NO_WEBSITE) == 80

    def test_slow_load_adds_bonus(self):
        biz = _make_business(
            rating=None,
            audit=_passing_audit(
                has_menu=False,  # 1 DOM failure -> +2
                load_time_seconds=8.5,  # > 5s threshold
            ),
        )
        # Base 40 (missing_features) + 2 (1 DOM) + 3 (slow load) = 45.
        assert _compute_score(biz, LeadTier.MISSING_FEATURES) == 45

    def test_broken_assets_add_bonus(self):
        biz = _make_business(
            rating=None,
            audit=_passing_audit(
                has_menu=False,
                broken_assets=[
                    {"url": "x", "status": 404, "type": "image"}
                ],
            ),
        )
        # Base 40 + 2 DOM + 2 broken-assets = 44.
        assert _compute_score(biz, LeadTier.MISSING_FEATURES) == 44

    def test_score_capped_at_100(self):
        # Pile every possible bonus on; verify the cap holds.
        biz = _make_business(
            url_classification=UrlClassification.NONE,
            website="", rating=5.0,
            audit=_passing_audit(
                lighthouse_mobile={
                    "performance": 5, "accessibility": 90,
                    "seo": 90, "best_practices": 90,
                },
                has_menu=False, has_hours=False, has_contact_info=False,
                has_ssl=False, has_online_ordering=False,
                has_reservation=False,
                load_time_seconds=20.0,
                broken_assets=[
                    {"url": "x", "status": 404, "type": "image"}
                ],
            ),
        )
        score = _compute_score(biz, LeadTier.NO_WEBSITE)
        # Sum exceeds 100 by a wide margin; verify cap.
        assert score == 100

    def test_modifier_constants_drive_value(self):
        # Sanity check that constants from config feed into the score.
        # If someone tweaks SCORE_PER_DOM_FAILURE in config, this test
        # naturally adjusts because we use the constant directly.
        biz = _make_business(
            rating=None,
            audit=_passing_audit(has_menu=False, has_hours=False),
        )
        expected = (
            40  # missing_features base
            + 2 * SCORE_PER_DOM_FAILURE
        )
        assert _compute_score(biz, LeadTier.MISSING_FEATURES) == expected


# ---------------------------------------------------------------------------
# _build_reasons
# ---------------------------------------------------------------------------


class TestBuildReasons:
    def test_no_website_classification_yields_no_website_reason(self):
        biz = _make_business(
            url_classification=UrlClassification.NONE, audit=None
        )
        reasons = _build_reasons(biz)
        assert reasons == ["No website found"]

    def test_social_media_classification_yields_social_reason(self):
        biz = _make_business(
            url_classification=UrlClassification.SOCIAL_MEDIA, audit=None
        )
        reasons = _build_reasons(biz)
        assert reasons == ["Only social media presence"]

    def test_directory_classification_yields_directory_reason(self):
        biz = _make_business(
            url_classification=UrlClassification.DIRECTORY_LISTING,
            audit=None,
        )
        reasons = _build_reasons(biz)
        assert reasons == ["Only directory listing"]

    def test_audit_deficiencies_appended_directly(self):
        # Reuse audit's own deficiency strings (cohesion) rather than
        # re-deriving with different wording.
        biz = _make_business(
            audit=_passing_audit(
                has_menu=False,
                has_ssl=False,
                deficiencies=["No SSL certificate", "No menu page found"],
            )
        )
        reasons = _build_reasons(biz)
        assert "No SSL certificate" in reasons
        assert "No menu page found" in reasons

    def test_slow_load_appended_when_threshold_exceeded(self):
        biz = _make_business(
            audit=_passing_audit(
                load_time_seconds=8.2,
                deficiencies=[],
            )
        )
        reasons = _build_reasons(biz)
        assert "Slow load time: 8.2s" in reasons

    def test_no_slow_load_reason_when_under_threshold(self):
        biz = _make_business(
            audit=_passing_audit(
                load_time_seconds=2.5,
                deficiencies=[],
            )
        )
        reasons = _build_reasons(biz)
        assert not any("Slow load" in r for r in reasons)


# ---------------------------------------------------------------------------
# score_leads (in-place mutation)
# ---------------------------------------------------------------------------


class TestScoreLeads:
    def test_mutates_each_business_lead(self):
        bizs = [
            _make_business(place_id="a", url_classification=UrlClassification.NONE,
                           rating=4.6, audit=None),
            _make_business(place_id="b", audit=_passing_audit()),
        ]
        result = score_leads(bizs)
        # Mutate-and-return-same-list pattern (matches discover_urls / audit_websites).
        assert result is bizs
        for b in bizs:
            assert b.lead is not None
            assert isinstance(b.lead, Lead)


# ---------------------------------------------------------------------------
# rank_leads (sort)
# ---------------------------------------------------------------------------


class TestRankLeads:
    def test_descending_by_score(self):
        # Spec test 4: three businesses with different scores -> sorted.
        a = _make_business(place_id="a", url_classification=UrlClassification.NONE,
                           rating=4.6, audit=None)
        b = _make_business(place_id="b", audit=_passing_audit(has_menu=False))
        c = _make_business(place_id="c", audit=_passing_audit())
        score_leads([a, b, c])
        ranked = rank_leads([a, b, c])
        # a should rank highest (no website, top rating).
        # c should rank lowest (skip tier, score 0).
        assert ranked[0].place_id == "a"
        assert ranked[-1].place_id == "c"
        # Scores should be in non-increasing order.
        scores = [b.lead.score for b in ranked]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


class TestExportToCsv:
    def test_writes_expected_columns(self, tmp_path):
        biz = _make_business(
            url_classification=UrlClassification.NONE,
            website="",
            rating=4.6,
            audit=None,
        )
        score_leads([biz])
        path = tmp_path / "out.csv"
        export_to_csv([biz], path)

        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 1
        assert list(rows[0].keys()) == list(CSV_COLUMNS)
        assert rows[0]["rank"] == "1"
        assert rows[0]["name"] == "Test Cafe"
        assert rows[0]["tier"] == "no_website"
        assert rows[0]["score"] == "100"

    def test_skip_tier_excluded(self, tmp_path):
        # Skip-tier entries shouldn't appear in the leads CSV.
        no_site = _make_business(
            place_id="a",
            url_classification=UrlClassification.NONE,
            audit=None,
        )
        clean = _make_business(place_id="b", audit=_passing_audit())
        score_leads([no_site, clean])

        path = tmp_path / "out.csv"
        export_to_csv([no_site, clean], path)

        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 1
        assert rows[0]["name"] == "Test Cafe"  # only the no-site one survives
        assert rows[0]["tier"] == "no_website"

    def test_reasons_joined_with_semicolons(self, tmp_path):
        biz = _make_business(
            url_classification=UrlClassification.NONE,
            audit=_passing_audit(
                has_menu=False,
                deficiencies=["No menu page found"],
            ),
        )
        score_leads([biz])
        path = tmp_path / "out.csv"
        export_to_csv([biz], path)

        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))

        # Multiple reasons joined with "; ".
        assert "; " in rows[0]["reasons"]
        # Reasons include both the no-website headline and audit deficiencies.
        assert "No website found" in rows[0]["reasons"]
        assert "No menu page found" in rows[0]["reasons"]

    def test_empty_input_writes_only_headers(self, tmp_path):
        path = tmp_path / "empty.csv"
        export_to_csv([], path)
        # Header line only.
        text = path.read_text(encoding="utf-8")
        assert text.strip() == ",".join(CSV_COLUMNS)


class TestCsvPathForDataFile:
    def test_filename_format(self):
        # `data/foo.json` -> `data/leads_foo_<today>.csv`
        result = csv_path_for_data_file(Path("data/santa_rosa_beach_fl.json"))
        # Just check the structure; the date is today's UTC date.
        assert result.parent == Path("data")
        assert result.name.startswith("leads_santa_rosa_beach_fl_")
        assert result.suffix == ".csv"


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------


class TestExportToMarkdown:
    def test_writes_header_and_summary_table(self, tmp_path):
        # Spec: header includes location label, scan date, totals;
        # tier table lists every tier with its count.
        no_site = _make_business(
            place_id="A",
            name="NoSiteBiz",
            url_classification=UrlClassification.NONE,
            audit=None,
        )
        clean = _make_business(place_id="B", name="CleanBiz", audit=_passing_audit())
        score_leads([no_site, clean])

        path = tmp_path / "report.md"
        export_to_markdown(
            [no_site, clean], path, location_label="Test City, ST"
        )

        text = path.read_text(encoding="utf-8")
        assert "# LeadScout: Test City, ST" in text
        assert "Total businesses scanned:** 2" in text
        # Active leads excludes skip-tier; only NoSiteBiz qualifies.
        assert "Active leads (above skip tier):** 1" in text
        # Tier table lists every tier in display order.
        assert "| no_website | 1 |" in text
        assert "| failing_audit | 0 |" in text
        assert "| missing_features | 0 |" in text
        assert "| skip | 1 |" in text

    def test_lists_active_leads_with_full_detail(self, tmp_path):
        biz = _make_business(
            place_id="A",
            name="The Cafe",
            address="100 Main St",
            phone="555-0000",
            website="",
            rating=4.6,
            url_classification=UrlClassification.NONE,
            audit=None,
        )
        score_leads([biz])
        path = tmp_path / "report.md"
        export_to_markdown([biz], path)

        text = path.read_text(encoding="utf-8")
        # Heading uses rank, name, score, tier.
        assert "### 1. The Cafe — score 100 — `no_website`" in text
        # Per-business detail bullets.
        assert "- **Address:** 100 Main St" in text
        assert "- **Phone:** 555-0000" in text
        # Empty website shown as em-dash placeholder, not blank.
        assert "- **Website:** —" in text
        assert "- **Rating:** 4.6" in text
        # Reasons listed under nested bullets.
        assert "  - No website found" in text

    def test_skip_tier_excluded_from_active_leads_section(self, tmp_path):
        # Skip-tier businesses count in the tier-summary table but don't
        # get individual entries in the Active leads section.
        skip = _make_business(
            place_id="A", name="CleanBiz", audit=_passing_audit()
        )
        score_leads([skip])
        path = tmp_path / "report.md"
        export_to_markdown([skip], path)

        text = path.read_text(encoding="utf-8")
        # No detail section for the skipped business.
        assert "### 1. CleanBiz" not in text
        # The empty-state line shows up.
        assert "_No leads above skip tier._" in text
        # Tier summary still counts it.
        assert "| skip | 1 |" in text

    def test_falls_back_to_path_stem_when_no_label(self, tmp_path):
        # When the caller doesn't pass location_label (e.g., `score`
        # CLI subcommand), the title falls back to the file's stem.
        biz = _make_business(
            url_classification=UrlClassification.NONE, audit=None
        )
        score_leads([biz])
        path = tmp_path / "santa_rosa_beach_fl_report.md"
        export_to_markdown([biz], path)

        text = path.read_text(encoding="utf-8")
        assert "# LeadScout: santa_rosa_beach_fl_report" in text


class TestMarkdownPathForDataFile:
    def test_filename_format(self):
        # `data/foo.json` -> `data/leads_foo_<today>.md`
        result = markdown_path_for_data_file(
            Path("data/santa_rosa_beach_fl.json")
        )
        assert result.parent == Path("data")
        assert result.name.startswith("leads_santa_rosa_beach_fl_")
        assert result.suffix == ".md"
