"""Tests for campaign.run_campaign and helpers (feature 06)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from leadscout.campaign import (
    JobSpec,
    load_plan,
    run_campaign,
    select_todays_jobs,
)
from leadscout.exceptions import LeadScoutError
from leadscout.models import Business, Lead, LeadTier, UrlClassification, UrlSource
from leadscout.storage import data_path_for_location, save_data

FIXTURE_PLAN = Path(__file__).parent / "fixtures" / "campaign_minimal.toml"


def _make_business(
    place_id: str,
    business_type: str,
    *,
    last_scanned: datetime | None = None,
) -> Business:
    """Stamp a business with a specific business_type + last_scanned so
    freshness-skip tests can drive `select_todays_jobs`."""
    return Business(
        place_id=place_id,
        name=f"Biz {place_id}",
        business_type=business_type,
        review_count=10,
        url_source=UrlSource.GOOGLE_PLACES,
        url_classification=UrlClassification.OFFICIAL_SITE,
        last_scanned=last_scanned or datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# load_plan
# ---------------------------------------------------------------------------


class TestLoadPlan:
    def test_cross_products_locations_categories(self):
        jobs, _ = load_plan(FIXTURE_PLAN)
        # 2 locations x 2 categories = 4 jobs.
        assert len(jobs) == 4
        # Each job is a (location, single category) pair.
        pairs = [(j.location, j.category) for j in jobs]
        assert ("Santa Rosa Beach, FL", "restaurant") in pairs
        assert ("Santa Rosa Beach, FL", "dentist") in pairs
        assert ("Destin, FL", "gym") in pairs
        assert ("Destin, FL", "spa") in pairs

    def test_defaults_propagate_to_jobspec_radius(self):
        # Fixture sets radius=4000 in [defaults], so every JobSpec
        # inherits that absent a per-job override.
        jobs, defaults = load_plan(FIXTURE_PLAN)
        assert defaults.get("radius") == 4000
        assert all(j.radius == 4000 for j in jobs)

    def test_warns_on_unknown_category(self, tmp_path, caplog):
        # Build a plan with a deliberately unknown category. The plan
        # parser should warn but still produce the JobSpec.
        plan = tmp_path / "p.toml"
        plan.write_text(
            '[[jobs]]\n'
            'location = "Test, FL"\n'
            'categories = ["definitely_not_a_real_type"]\n',
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            jobs, _ = load_plan(plan)
        assert len(jobs) == 1
        assert any(
            "definitely_not_a_real_type" in rec.message for rec in caplog.records
        )

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_plan(tmp_path / "nope.toml")

    def test_malformed_toml_raises_leadscout_error(self, tmp_path):
        plan = tmp_path / "bad.toml"
        plan.write_text("this = is = not valid", encoding="utf-8")
        with pytest.raises(LeadScoutError):
            load_plan(plan)

    def test_job_without_location_raises(self, tmp_path):
        plan = tmp_path / "p.toml"
        plan.write_text(
            '[[jobs]]\ncategories = ["restaurant"]\n',
            encoding="utf-8",
        )
        with pytest.raises(LeadScoutError):
            load_plan(plan)


# ---------------------------------------------------------------------------
# select_todays_jobs
# ---------------------------------------------------------------------------


class TestSelectTodaysJobs:
    def test_includes_never_scanned_locations(self, tmp_path):
        # Fresh tmp data dir: no JSON files exist for either location,
        # so every job runs.
        jobs = [
            JobSpec("Townville, FL", "restaurant", 5000),
            JobSpec("Othertown, FL", "dentist", 5000),
        ]
        todo, skipped = select_todays_jobs(jobs, tmp_path, refresh_days=7)
        assert todo == jobs
        assert skipped == []

    def test_skips_fresh_combo(self, tmp_path):
        # Pre-seed a JSON for the location whose business_type matches
        # the job's category, with a recent last_scanned. select_todays_jobs
        # should mark that combo as fresh and skip it.
        path = data_path_for_location(tmp_path, "Townville, FL")
        save_data(path, [_make_business("A", "restaurant")])
        jobs = [JobSpec("Townville, FL", "restaurant", 5000)]
        todo, skipped = select_todays_jobs(jobs, tmp_path, refresh_days=7)
        assert todo == []
        assert skipped == jobs

    def test_includes_stale_combo(self, tmp_path):
        # Same setup but last_scanned is 30 days ago — past the 7-day
        # window, so the combo should re-run.
        stale = datetime.now(timezone.utc) - timedelta(days=30)
        path = data_path_for_location(tmp_path, "Townville, FL")
        save_data(path, [_make_business("A", "restaurant", last_scanned=stale)])
        jobs = [JobSpec("Townville, FL", "restaurant", 5000)]
        todo, skipped = select_todays_jobs(jobs, tmp_path, refresh_days=7)
        assert todo == jobs
        assert skipped == []

    def test_includes_when_category_not_yet_scanned(self, tmp_path):
        # Location was scanned for "restaurant" but not for "dentist".
        # The "dentist" job should run; the "restaurant" one should skip.
        path = data_path_for_location(tmp_path, "Townville, FL")
        save_data(path, [_make_business("A", "restaurant")])
        jobs = [
            JobSpec("Townville, FL", "restaurant", 5000),
            JobSpec("Townville, FL", "dentist", 5000),
        ]
        todo, skipped = select_todays_jobs(jobs, tmp_path, refresh_days=7)
        assert [j.category for j in todo] == ["dentist"]
        assert [j.category for j in skipped] == ["restaurant"]

    def test_includes_when_any_match_is_stale(self, tmp_path):
        # Location has two restaurants on disk: one fresh, one stale.
        # Combo is "not fresh" because at least one match is stale.
        stale = datetime.now(timezone.utc) - timedelta(days=30)
        path = data_path_for_location(tmp_path, "Townville, FL")
        save_data(path, [
            _make_business("Fresh", "restaurant"),
            _make_business("Stale", "restaurant", last_scanned=stale),
        ])
        jobs = [JobSpec("Townville, FL", "restaurant", 5000)]
        todo, skipped = select_todays_jobs(jobs, tmp_path, refresh_days=7)
        assert todo == jobs


# ---------------------------------------------------------------------------
# run_campaign
# ---------------------------------------------------------------------------


def _patch_pipeline(monkeypatch, *, calls: list[str]):
    """Replace run_pipeline with a recording fake. Mirrors the real
    pipeline's merge behavior (load existing + append new + save) so
    successive jobs targeting the same location accumulate, instead of
    each one overwriting the previous job's persisted business."""
    from leadscout.storage import load_data

    def fake_run_pipeline(location, radius, categories, data_dir, **kwargs):
        calls.append(f"{location}/{categories[0]}")
        biz = _make_business(f"{location}-{categories[0]}", categories[0])
        biz.lead = Lead(tier=LeadTier.NO_WEBSITE, score=80, reasons=[])
        path = data_path_for_location(data_dir, location)
        # Merge by union: existing records survive, new business appended.
        # Real run_pipeline goes through merge_business + place_id keying;
        # for test purposes a simple list-extend is sufficient because
        # the fake business's place_id is unique per (location, category).
        existing = load_data(path) if path.exists() else []
        save_data(path, existing + [biz])
        return existing + [biz]

    monkeypatch.setattr("leadscout.campaign.run_pipeline", fake_run_pipeline)


class TestRunCampaign:
    def test_runs_all_jobs_when_quota_ample(self, monkeypatch, tmp_path):
        calls: list[str] = []
        _patch_pipeline(monkeypatch, calls=calls)
        summary = run_campaign(
            FIXTURE_PLAN, tmp_path,
            places_key="pk", cs_key=None, cs_cx=None, psi_key=None,
        )
        assert summary.jobs_total == 4
        assert summary.jobs_run == 4
        assert summary.jobs_remaining == 0
        assert summary.halted_reason == "all jobs complete"
        # Every job dispatched in plan order across both locations.
        assert len(calls) == 4

    def test_dry_run_invokes_no_pipeline(self, monkeypatch, tmp_path):
        calls: list[str] = []
        _patch_pipeline(monkeypatch, calls=calls)
        summary = run_campaign(
            FIXTURE_PLAN, tmp_path,
            places_key="pk", cs_key=None, cs_cx=None, psi_key=None,
            dry_run=True,
        )
        assert calls == []
        assert summary.halted_reason and "dry-run" in summary.halted_reason
        assert summary.jobs_run == 0
        # The full queue is reported as "remaining" so the user can see
        # what tomorrow's run would tackle.
        assert summary.jobs_remaining == 4
        assert len(summary.queued_jobs) == 4

    def test_max_jobs_caps_execution(self, monkeypatch, tmp_path):
        calls: list[str] = []
        _patch_pipeline(monkeypatch, calls=calls)
        summary = run_campaign(
            FIXTURE_PLAN, tmp_path,
            places_key="pk", cs_key=None, cs_cx=None, psi_key=None,
            max_jobs=2,
        )
        assert summary.jobs_run == 2
        assert summary.jobs_remaining == 2
        assert summary.halted_reason and "max-jobs" in summary.halted_reason

    def test_halts_on_places_quota_exhausted(self, monkeypatch, tmp_path):
        # Pre-seed the Places quota file at the safe limit so the very
        # first job sees `quota.remaining < 2` and the loop bails.
        import json

        from leadscout.config import PLACES_SAFE_LIMIT
        from leadscout.places_quota import QUOTA_FILENAME

        today = datetime.now(timezone.utc).date().isoformat()
        (tmp_path / QUOTA_FILENAME).write_text(
            json.dumps({"date": today, "count": PLACES_SAFE_LIMIT}),
            encoding="utf-8",
        )

        calls: list[str] = []
        _patch_pipeline(monkeypatch, calls=calls)
        summary = run_campaign(
            FIXTURE_PLAN, tmp_path,
            places_key="pk", cs_key=None, cs_cx=None, psi_key=None,
        )
        assert summary.jobs_run == 0
        assert summary.jobs_remaining == 4
        assert summary.halted_reason and "Places quota" in summary.halted_reason

    def test_skips_fresh_jobs_on_re_run(self, monkeypatch, tmp_path):
        # First run: everything executes and persists fresh records.
        calls: list[str] = []
        _patch_pipeline(monkeypatch, calls=calls)
        summary1 = run_campaign(
            FIXTURE_PLAN, tmp_path,
            places_key="pk", cs_key=None, cs_cx=None, psi_key=None,
        )
        assert summary1.jobs_run == 4

        # Second run with the same plan: every (location, category) is
        # now fresh; nothing should execute, all 4 land in skipped_fresh.
        calls.clear()
        summary2 = run_campaign(
            FIXTURE_PLAN, tmp_path,
            places_key="pk", cs_key=None, cs_cx=None, psi_key=None,
        )
        assert calls == []
        assert summary2.jobs_skipped_fresh == 4
        assert summary2.jobs_run == 0
