"""Tests for pipeline.run_pipeline (feature 06).

Exercises the orchestration body that used to live inline in `cli.run`.
Stage functions are monkeypatched on the pipeline module so the tests
don't hit the network. We assert:
- Stage ordering (search -> filter -> discover -> audit -> score).
- The filter is invoked between search and discover.
- Soft-fail to reclassify_urls when Custom Search creds are absent.
- A PlacesQuotaTracker is threaded through to `search_places`.
"""

from datetime import datetime, timezone

from leadscout.models import Business, Lead, LeadTier, UrlClassification, UrlSource
from leadscout.pipeline import run_pipeline


def _make(name: str, review_count: int) -> Business:
    return Business(
        place_id=name,
        name=name,
        address="123 Main St, Townsville, FL 32459, USA",
        review_count=review_count,
        url_source=UrlSource.NONE,
        url_classification=UrlClassification.NONE,
        last_scanned=datetime.now(timezone.utc),
    )


def _attach_lead(b: Business) -> None:
    """Helper to give a business a Lead so subsequent rank/save steps
    don't trip over a None lead."""
    b.lead = Lead(tier=LeadTier.NO_WEBSITE, score=80, reasons=[])


def _patch_stages(monkeypatch, *, calls: list[str], search_returns: list[Business]):
    """Wire up fake search/discover/audit/score that record into `calls`."""
    def fake_search(location, radius, key, **kwargs):
        calls.append("search")
        return search_returns

    def fake_discover(businesses, *args, **kwargs):
        calls.append("discover")
        return businesses

    def fake_reclassify(businesses):
        calls.append("reclassify")
        return businesses

    def fake_audit(businesses, *args, **kwargs):
        calls.append("audit")
        return businesses

    def fake_score(businesses):
        calls.append("score")
        for b in businesses:
            _attach_lead(b)
        return businesses

    monkeypatch.setattr("leadscout.pipeline.search_places", fake_search)
    monkeypatch.setattr("leadscout.pipeline.discover_urls", fake_discover)
    monkeypatch.setattr("leadscout.pipeline.reclassify_urls", fake_reclassify)
    monkeypatch.setattr("leadscout.pipeline.audit_websites", fake_audit)
    monkeypatch.setattr("leadscout.pipeline.score_leads", fake_score)


class TestStageOrdering:
    def test_default_order_with_custom_search(self, monkeypatch, tmp_path):
        calls: list[str] = []
        _patch_stages(
            monkeypatch, calls=calls,
            search_returns=[_make("Active", 100)],
        )
        run_pipeline(
            "Townsville, FL", 5000, ["restaurant"], tmp_path,
            places_key="pk", cs_key="ck", cs_cx="cx", psi_key="psi",
            echo=False,
        )
        # Stage order: filter is silent (pure function), so the
        # external sequence is search -> discover -> audit -> score.
        assert calls == ["search", "discover", "audit", "score"]

    def test_skips_discover_when_cs_creds_missing(self, monkeypatch, tmp_path):
        calls: list[str] = []
        _patch_stages(
            monkeypatch, calls=calls,
            search_returns=[_make("Active", 100)],
        )
        run_pipeline(
            "Townsville, FL", 5000, ["restaurant"], tmp_path,
            places_key="pk", cs_key=None, cs_cx=None, psi_key="psi",
            echo=False,
        )
        # No discover call; reclassify ran in its place.
        assert calls == ["search", "reclassify", "audit", "score"]


class TestFilterIntegration:
    def test_filter_runs_between_search_and_discover(self, monkeypatch, tmp_path):
        # Search returns one keep + one drop. Discover should only see
        # the kept one. We assert that by capturing the businesses arg
        # in the fake_discover.
        captured: dict = {}

        def fake_search(*args, **kwargs):
            return [_make("Live", 100), _make("Dead", 0)]

        def fake_discover(businesses, *args, **kwargs):
            captured["discover_input"] = list(businesses)
            return businesses

        def fake_audit(businesses, *args, **kwargs):
            return businesses

        def fake_score(businesses):
            for b in businesses:
                _attach_lead(b)
            return businesses

        monkeypatch.setattr("leadscout.pipeline.search_places", fake_search)
        monkeypatch.setattr("leadscout.pipeline.discover_urls", fake_discover)
        monkeypatch.setattr("leadscout.pipeline.audit_websites", fake_audit)
        monkeypatch.setattr("leadscout.pipeline.score_leads", fake_score)

        run_pipeline(
            "Townsville, FL", 5000, ["restaurant"], tmp_path,
            places_key="pk", cs_key="ck", cs_cx="cx", psi_key="psi",
            min_review_count=1,
            echo=False,
        )
        names = [b.name for b in captured["discover_input"]]
        # Dead listing was filtered before discover; only "Live" survives.
        assert names == ["Live"]

    def test_default_filter_is_off(self, monkeypatch, tmp_path):
        # min_review_count defaults to 0 in run_pipeline (preserves the
        # prior `cli.run` behavior). Zero-review listings still flow.
        captured: dict = {}

        def fake_search(*args, **kwargs):
            return [_make("Dead", 0)]

        def fake_discover(businesses, *args, **kwargs):
            captured["discover_input"] = list(businesses)
            return businesses

        def fake_audit(businesses, *args, **kwargs):
            return businesses

        def fake_score(businesses):
            for b in businesses:
                _attach_lead(b)
            return businesses

        monkeypatch.setattr("leadscout.pipeline.search_places", fake_search)
        monkeypatch.setattr("leadscout.pipeline.discover_urls", fake_discover)
        monkeypatch.setattr("leadscout.pipeline.audit_websites", fake_audit)
        monkeypatch.setattr("leadscout.pipeline.score_leads", fake_score)

        run_pipeline(
            "Townsville, FL", 5000, ["restaurant"], tmp_path,
            places_key="pk", cs_key="ck", cs_cx="cx", psi_key="psi",
            echo=False,
        )
        # Dead listing reaches discover because the filter is off.
        assert [b.name for b in captured["discover_input"]] == ["Dead"]


class TestQuotaIntegration:
    def test_quota_tracker_threads_into_search(self, monkeypatch, tmp_path):
        # The pipeline should pass its `places_quota` argument straight
        # to `search_places` as the `quota=` kwarg.
        captured: dict = {}

        def fake_search(location, radius, key, **kwargs):
            captured["quota_kwarg"] = kwargs.get("quota")
            return [_make("Active", 100)]

        def fake_discover(businesses, *args, **kwargs):
            return businesses

        def fake_audit(businesses, *args, **kwargs):
            return businesses

        def fake_score(businesses):
            for b in businesses:
                _attach_lead(b)
            return businesses

        monkeypatch.setattr("leadscout.pipeline.search_places", fake_search)
        monkeypatch.setattr("leadscout.pipeline.discover_urls", fake_discover)
        monkeypatch.setattr("leadscout.pipeline.audit_websites", fake_audit)
        monkeypatch.setattr("leadscout.pipeline.score_leads", fake_score)

        # Sentinel value — we just want to confirm it survived the trip.
        sentinel = object()
        run_pipeline(
            "Townsville, FL", 5000, ["restaurant"], tmp_path,
            places_key="pk", cs_key="ck", cs_cx="cx", psi_key="psi",
            places_quota=sentinel,  # type: ignore[arg-type]
            echo=False,
        )
        assert captured["quota_kwarg"] is sentinel


class TestReturnValue:
    def test_returns_scored_businesses(self, monkeypatch, tmp_path):
        # The function returns the post-score list so callers can rank
        # / export without re-loading from disk.
        _patch_stages(
            monkeypatch,
            calls=[],
            search_returns=[_make("X", 100), _make("Y", 100)],
        )
        result = run_pipeline(
            "Townsville, FL", 5000, ["restaurant"], tmp_path,
            places_key="pk", cs_key=None, cs_cx=None, psi_key="psi",
            echo=False,
        )
        assert len(result) == 2
        assert all(b.lead is not None for b in result)

    def test_writes_per_location_json(self, monkeypatch, tmp_path):
        _patch_stages(
            monkeypatch,
            calls=[],
            search_returns=[_make("X", 100)],
        )
        run_pipeline(
            "Test Town, FL", 5000, ["restaurant"], tmp_path,
            places_key="pk", cs_key=None, cs_cx=None, psi_key="psi",
            echo=False,
        )
        assert (tmp_path / "test_town_fl.json").exists()
