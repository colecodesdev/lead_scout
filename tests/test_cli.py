"""Tests for the search & discover subcommand handlers in cli.py.

We use Click's CliRunner to invoke the command exactly as a user would,
without spawning a real subprocess. The service functions
(search_places, discover_urls) are monkeypatched to stubs so we
exercise the handler's logic (env reading, error formatting, slug
derivation, merge+save, summary output) in isolation from the HTTP
layer.
"""

from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from leadscout.cli import cli
from leadscout.exceptions import APIError
from leadscout.models import Business, UrlClassification, UrlSource
from leadscout.storage import load_data, save_data


def _make_business(**overrides) -> Business:
    """Mirror of the helper in test_storage.py, copied here to keep the
    test files independent. Defaults match a typical Places API result."""
    defaults = {
        "place_id": "abc123",
        "name": "Test Restaurant",
        "address": "123 Main St",
        "phone": "",
        "website": "https://example.com",
        "url_source": UrlSource.GOOGLE_PLACES,
        "url_classification": UrlClassification.OFFICIAL_SITE,
        "rating": 4.5,
        "review_count": 0,
        "business_type": "restaurant",
        "last_scanned": datetime(2026, 5, 3, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return Business(**defaults)


@pytest.fixture
def runner():
    """A fresh CliRunner per test; isolates filesystem and stdio captures."""
    return CliRunner()


class TestSearchCommand:
    def test_missing_api_key_exits_with_error(self, runner, monkeypatch, tmp_path):
        # Ensure the env var is absent. delenv with raising=False is a
        # no-op when the var doesn't exist, so the test works regardless
        # of the developer's local shell environment.
        monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)

        result = runner.invoke(
            cli,
            ["--data-dir", str(tmp_path), "search", "--location", "Anywhere, USA"],
        )

        # Non-zero exit signals failure to shells/pipelines.
        assert result.exit_code != 0
        # Error text should call out the missing variable so the user
        # knows what to fix without reading the source.
        assert "GOOGLE_PLACES_API_KEY" in result.output

    def test_happy_path_writes_slugified_file(self, runner, monkeypatch, tmp_path):
        # Provide an API key so the env check passes; the value is never
        # actually used because we stub search_places below.
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake")

        # Stub search_places to return a fixed list so we test the CLI
        # handler's wiring, not the HTTP layer (which has its own tests).
        sample = [
            _make_business(place_id="A", name="Alpha"),
            _make_business(place_id="B", name="Beta"),
        ]
        monkeypatch.setattr(
            "leadscout.cli.search_places", lambda *args, **kwargs: sample
        )

        result = runner.invoke(
            cli,
            [
                "--data-dir",
                str(tmp_path),
                "search",
                "--location",
                "Santa Rosa Beach, FL",
            ],
        )

        assert result.exit_code == 0, result.output
        # Slug is lowercase, non-word chars collapsed to "_", trailing
        # underscores stripped. "Santa Rosa Beach, FL" -> "santa_rosa_beach_fl".
        expected_path = tmp_path / "santa_rosa_beach_fl.json"
        assert expected_path.exists()
        # Loaded businesses should match what we returned.
        loaded = load_data(expected_path)
        assert {b.place_id for b in loaded} == {"A", "B"}
        # Summary should mention the count and "new" indicator.
        assert "Found 2 businesses" in result.output
        assert "(2 new)" in result.output

    def test_merges_into_existing_data(self, runner, monkeypatch, tmp_path):
        # First run creates the file. Second run with one overlapping
        # record (same place_id) and one fresh record should merge into
        # the existing file and report "1 new".
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake")

        first_run = [_make_business(place_id="A", name="Alpha")]
        second_run = [
            _make_business(place_id="A", name="Alpha Renamed"),
            _make_business(place_id="C", name="Gamma"),
        ]

        # iter() lets us return different lists across the two invocations
        # without the stub having to inspect call args.
        results_iter = iter([first_run, second_run])
        monkeypatch.setattr(
            "leadscout.cli.search_places",
            lambda *args, **kwargs: next(results_iter),
        )

        # Run 1
        runner.invoke(
            cli, ["--data-dir", str(tmp_path), "search", "--location", "Town, ST"]
        )
        # Run 2
        result = runner.invoke(
            cli, ["--data-dir", str(tmp_path), "search", "--location", "Town, ST"]
        )

        assert result.exit_code == 0, result.output
        assert "(1 new)" in result.output

        loaded = load_data(tmp_path / "town_st.json")
        by_id = {b.place_id: b for b in loaded}
        # Both place_ids present, with the renamed one's update applied.
        assert set(by_id) == {"A", "C"}
        assert by_id["A"].name == "Alpha Renamed"

    def test_apierror_surfaces_as_user_message(self, runner, monkeypatch, tmp_path):
        # search_places raising APIError should produce a clean stderr
        # message and a non-zero exit, not a Python traceback.
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake")

        def boom(*args, **kwargs):
            raise APIError("Geocoding returned no results for 'nowhere'")

        monkeypatch.setattr("leadscout.cli.search_places", boom)

        result = runner.invoke(
            cli,
            ["--data-dir", str(tmp_path), "search", "--location", "nowhere"],
        )

        assert result.exit_code != 0
        # The APIError message text should appear in the output verbatim.
        assert "Geocoding returned no results" in result.output


class TestDiscoverCommand:
    def _seed_data_file(self, tmp_path) -> str:
        """Write a small businesses JSON to disk and return its path."""
        path = tmp_path / "leads.json"
        save_data(
            path,
            [
                _make_business(
                    place_id="A",
                    name="Alpha",
                    website="",
                    url_source=UrlSource.NONE,
                    url_classification=UrlClassification.NONE,
                ),
                _make_business(
                    place_id="B",
                    name="Beta",
                    website="https://www.facebook.com/beta",
                    url_source=UrlSource.GOOGLE_PLACES,
                    url_classification=UrlClassification.OFFICIAL_SITE,
                ),
            ],
        )
        return str(path)

    def test_missing_env_vars_exit_with_error(
        self, runner, monkeypatch, tmp_path
    ):
        # Both Custom Search vars must be present; missing either should
        # produce a clear error.
        monkeypatch.delenv("GOOGLE_CUSTOM_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CUSTOM_SEARCH_CX", raising=False)

        data_file = self._seed_data_file(tmp_path)
        result = runner.invoke(
            cli,
            ["--data-dir", str(tmp_path), "discover", "--data-file", data_file],
        )

        assert result.exit_code != 0
        assert "GOOGLE_CUSTOM_SEARCH_API_KEY" in result.output

    def test_happy_path_calls_discover_urls_and_saves(
        self, runner, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_API_KEY", "fake")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_CX", "fake-cx")

        data_file = self._seed_data_file(tmp_path)

        # Stub discover_urls to mutate the input list (simulates a real
        # discovery for "Alpha" + reclassification of "Beta").
        def fake_discover(businesses, api_key, cx, *, data_dir, force):
            for b in businesses:
                if b.place_id == "A":
                    b.website = "https://alpha.example.com"
                    b.url_source = UrlSource.SEARCH_DISCOVERED
                    b.url_classification = UrlClassification.OFFICIAL_SITE
                elif b.place_id == "B":
                    b.url_classification = UrlClassification.SOCIAL_MEDIA
            return businesses

        monkeypatch.setattr("leadscout.cli.discover_urls", fake_discover)

        result = runner.invoke(
            cli,
            ["--data-dir", str(tmp_path), "discover", "--data-file", data_file],
        )

        assert result.exit_code == 0, result.output
        # Summary line should reflect the changes.
        assert "1 URLs discovered" in result.output
        # Reload from disk to confirm save_data was called with the
        # mutated list.
        loaded = load_data(tmp_path / "leads.json")
        by_id = {b.place_id: b for b in loaded}
        assert by_id["A"].url_source == UrlSource.SEARCH_DISCOVERED
        assert by_id["B"].url_classification == UrlClassification.SOCIAL_MEDIA

    def test_force_flag_propagates(self, runner, monkeypatch, tmp_path):
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_API_KEY", "fake")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_CX", "fake-cx")

        data_file = self._seed_data_file(tmp_path)
        captured: dict = {}

        def fake_discover(businesses, api_key, cx, *, data_dir, force):
            captured["force"] = force
            return businesses

        monkeypatch.setattr("leadscout.cli.discover_urls", fake_discover)

        runner.invoke(
            cli,
            [
                "--data-dir",
                str(tmp_path),
                "discover",
                "--data-file",
                data_file,
                "--force",
            ],
        )

        assert captured["force"] is True

    def test_apierror_surfaces_as_user_message(
        self, runner, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_API_KEY", "fake")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_CX", "fake-cx")

        data_file = self._seed_data_file(tmp_path)

        def boom(*args, **kwargs):
            raise APIError("Custom Search auth failure (HTTP 401)")

        monkeypatch.setattr("leadscout.cli.discover_urls", boom)

        result = runner.invoke(
            cli,
            ["--data-dir", str(tmp_path), "discover", "--data-file", data_file],
        )

        assert result.exit_code != 0
        assert "auth failure" in result.output


class TestAuditCommand:
    def _seed_data_file(self, tmp_path) -> str:
        path = tmp_path / "leads.json"
        save_data(
            path,
            [
                _make_business(
                    place_id="A",
                    name="Alpha",
                    website="https://alpha.example.com",
                    url_classification=UrlClassification.OFFICIAL_SITE,
                ),
                _make_business(
                    place_id="B",
                    name="Beta",
                    website="https://www.facebook.com/beta",
                    url_classification=UrlClassification.SOCIAL_MEDIA,
                ),
            ],
        )
        return str(path)

    def test_uses_pagespeed_key_when_set(self, runner, monkeypatch, tmp_path):
        # GOOGLE_PAGESPEED_API_KEY takes precedence over PLACES key.
        monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "psi-key")
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-key")
        captured: dict = {}

        def fake_audit(businesses, api_key, *, force):
            captured["api_key"] = api_key
            return businesses

        monkeypatch.setattr("leadscout.cli.audit_websites", fake_audit)
        data_file = self._seed_data_file(tmp_path)

        result = runner.invoke(
            cli,
            ["--data-dir", str(tmp_path), "audit", "--data-file", data_file],
        )

        assert result.exit_code == 0, result.output
        assert captured["api_key"] == "psi-key"

    def test_falls_back_to_places_key_when_psi_unset(
        self, runner, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("GOOGLE_PAGESPEED_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-key")
        captured: dict = {}

        def fake_audit(businesses, api_key, *, force):
            captured["api_key"] = api_key
            return businesses

        monkeypatch.setattr("leadscout.cli.audit_websites", fake_audit)
        data_file = self._seed_data_file(tmp_path)

        runner.invoke(
            cli,
            ["--data-dir", str(tmp_path), "audit", "--data-file", data_file],
        )
        assert captured["api_key"] == "places-key"

    def test_unauthenticated_when_no_keys_set(
        self, runner, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("GOOGLE_PAGESPEED_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
        captured: dict = {}

        def fake_audit(businesses, api_key, *, force):
            captured["api_key"] = api_key
            return businesses

        monkeypatch.setattr("leadscout.cli.audit_websites", fake_audit)
        data_file = self._seed_data_file(tmp_path)

        result = runner.invoke(
            cli,
            ["--data-dir", str(tmp_path), "audit", "--data-file", data_file],
        )
        # api_key should be None (unauthenticated).
        assert captured["api_key"] is None
        # Heads-up message should mention the lower quota.
        assert "unauthenticated" in result.output

    def test_force_flag_propagates(self, runner, monkeypatch, tmp_path):
        monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "fake")
        captured: dict = {}

        def fake_audit(businesses, api_key, *, force):
            captured["force"] = force
            return businesses

        monkeypatch.setattr("leadscout.cli.audit_websites", fake_audit)
        data_file = self._seed_data_file(tmp_path)

        runner.invoke(
            cli,
            [
                "--data-dir", str(tmp_path), "audit",
                "--data-file", data_file, "--force",
            ],
        )
        assert captured["force"] is True

    def test_audit_summary_reports_counts(self, runner, monkeypatch, tmp_path):
        # Stub audit_websites to attach a populated Audit so we can verify
        # the summary line tallies audited count + deficiencies correctly.
        monkeypatch.setenv("GOOGLE_PAGESPEED_API_KEY", "fake")

        from leadscout.models import Audit

        def fake_audit(businesses, api_key, *, force):
            for b in businesses:
                if b.url_classification == UrlClassification.OFFICIAL_SITE:
                    b.audit = Audit(
                        audited_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
                        deficiencies=["No SSL certificate", "No menu page found"],
                    )
            return businesses

        monkeypatch.setattr("leadscout.cli.audit_websites", fake_audit)
        data_file = self._seed_data_file(tmp_path)

        result = runner.invoke(
            cli,
            ["--data-dir", str(tmp_path), "audit", "--data-file", data_file],
        )
        assert result.exit_code == 0, result.output
        # 1 official + 1 social: only the official one was audited.
        assert "1 businesses audited" in result.output
        assert "2 total deficiencies" in result.output


class TestScoreCommand:
    def _seed_data_file(self, tmp_path) -> str:
        path = tmp_path / "leads.json"
        save_data(
            path,
            [
                _make_business(
                    place_id="A",
                    name="NoSiteBiz",
                    website="",
                    url_classification=UrlClassification.NONE,
                    rating=4.6,
                ),
                _make_business(
                    place_id="B",
                    name="CleanBiz",
                    website="https://clean.example.com",
                    url_classification=UrlClassification.OFFICIAL_SITE,
                ),
            ],
        )
        return str(path)

    def test_score_writes_lead_to_each_business(
        self, runner, monkeypatch, tmp_path
    ):
        data_file = self._seed_data_file(tmp_path)
        result = runner.invoke(
            cli,
            ["--data-dir", str(tmp_path), "score", "--data-file", data_file],
        )

        assert result.exit_code == 0, result.output
        # Reload to confirm Lead was attached and persisted.
        from leadscout.storage import load_data

        loaded = load_data(tmp_path / "leads.json")
        for b in loaded:
            assert b.lead is not None

    def test_score_summary_excludes_skip_tier(
        self, runner, monkeypatch, tmp_path
    ):
        # CleanBiz (no audit) -> SKIP. NoSiteBiz -> NO_WEBSITE.
        # Summary should show only the no-website business.
        data_file = self._seed_data_file(tmp_path)
        result = runner.invoke(
            cli,
            ["--data-dir", str(tmp_path), "score", "--data-file", data_file],
        )
        assert "NoSiteBiz" in result.output
        assert "CleanBiz" not in result.output

    def test_export_csv_writes_file(self, runner, monkeypatch, tmp_path):
        data_file = self._seed_data_file(tmp_path)
        result = runner.invoke(
            cli,
            [
                "--data-dir", str(tmp_path), "score",
                "--data-file", data_file, "--export", "csv",
            ],
        )
        assert result.exit_code == 0, result.output
        # CSV file written to the data file's parent dir, named with today's UTC date.
        csv_files = list(tmp_path.glob("leads_*.csv"))
        assert len(csv_files) == 1
        # Confirm header and at least one data row.
        import csv as _csv

        with open(csv_files[0], encoding="utf-8", newline="") as f:
            rows = list(_csv.DictReader(f))
        assert len(rows) >= 1
        assert "rank" in rows[0]
        assert "tier" in rows[0]


class TestRunCommand:
    def test_run_chains_all_four_stages(
        self, runner, monkeypatch, tmp_path
    ):
        # Set every required env var so the upfront check passes.
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-key")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_API_KEY", "cs-key")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_CX", "cs-cx")

        # Track which stages were called and in what order so we
        # validate the chaining contract (search -> discover -> audit -> score).
        stages_called: list[str] = []

        def fake_search(location, radius, key, **kwargs):
            stages_called.append("search")
            return [
                _make_business(
                    place_id="A",
                    name="Found",
                    website="",
                    url_classification=UrlClassification.NONE,
                )
            ]

        def fake_discover(businesses, api_key, cx, *, data_dir, force):
            stages_called.append("discover")
            return businesses

        def fake_audit(businesses, api_key, *, force):
            stages_called.append("audit")
            return businesses

        def fake_score(businesses):
            stages_called.append("score")
            from leadscout.models import Lead, LeadTier
            for b in businesses:
                b.lead = Lead(
                    tier=LeadTier.NO_WEBSITE, score=80,
                    reasons=["No website found"],
                )
            return businesses

        monkeypatch.setattr("leadscout.cli.search_places", fake_search)
        monkeypatch.setattr("leadscout.cli.discover_urls", fake_discover)
        monkeypatch.setattr("leadscout.cli.audit_websites", fake_audit)
        monkeypatch.setattr("leadscout.cli.score_leads", fake_score)

        result = runner.invoke(
            cli,
            [
                "--data-dir", str(tmp_path), "run",
                "--location", "Test Town, ST",
            ],
        )

        assert result.exit_code == 0, result.output
        assert stages_called == ["search", "discover", "audit", "score"]
        # Slugified data file written to disk.
        assert (tmp_path / "test_town_st.json").exists()

    def test_run_fails_fast_when_places_key_missing(
        self, runner, monkeypatch, tmp_path
    ):
        # Places is the entry point; without it nothing else can run.
        # Hard-fail with a clear message rather than starting the pipeline.
        monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_API_KEY", "cs-key")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_CX", "cs-cx")

        result = runner.invoke(
            cli,
            [
                "--data-dir", str(tmp_path), "run",
                "--location", "Test Town, ST",
            ],
        )
        assert result.exit_code != 0
        assert "GOOGLE_PLACES_API_KEY" in result.output

    def test_run_skips_discovery_when_custom_search_env_missing(
        self, runner, monkeypatch, tmp_path
    ):
        # Custom Search is optional. Missing env vars -> skip discovery,
        # call reclassify_urls instead (local-only domain classification),
        # continue with audit + score.
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-key")
        monkeypatch.delenv("GOOGLE_CUSTOM_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CUSTOM_SEARCH_CX", raising=False)

        called: list[str] = []

        def fake_search(location, radius, key, **kwargs):
            called.append("search")
            return [
                _make_business(
                    place_id="A",
                    url_classification=UrlClassification.NONE,
                )
            ]

        def fake_discover(*args, **kwargs):
            called.append("discover")
            return args[0]

        def fake_reclassify(businesses):
            called.append("reclassify")
            return businesses

        def fake_audit(businesses, *args, **kwargs):
            called.append("audit")
            return businesses

        def fake_score(businesses):
            from leadscout.models import Lead, LeadTier

            called.append("score")
            for b in businesses:
                b.lead = Lead(tier=LeadTier.NO_WEBSITE, score=80, reasons=[])
            return businesses

        monkeypatch.setattr("leadscout.cli.search_places", fake_search)
        monkeypatch.setattr("leadscout.cli.discover_urls", fake_discover)
        monkeypatch.setattr("leadscout.cli.reclassify_urls", fake_reclassify)
        monkeypatch.setattr("leadscout.cli.audit_websites", fake_audit)
        monkeypatch.setattr("leadscout.cli.score_leads", fake_score)

        result = runner.invoke(
            cli,
            [
                "--data-dir", str(tmp_path), "run",
                "--location", "Test Town, ST",
            ],
        )

        assert result.exit_code == 0, result.output
        # discover NOT called; reclassify ran in its place; audit + score continued.
        assert called == ["search", "reclassify", "audit", "score"]
        assert "SKIPPED" in result.output

    def test_run_falls_back_when_custom_search_raises_apierror(
        self, runner, monkeypatch, tmp_path
    ):
        # Custom Search env vars present, but the API returns a runtime
        # denial (e.g., "project does not have access" 403 for new GCP
        # projects). Run should catch the APIError, log, and fall back
        # to reclassify_urls so audit + score still execute.
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-key")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_API_KEY", "cs-key")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_CX", "cs-cx")

        called: list[str] = []

        def fake_search(location, radius, key, **kwargs):
            called.append("search")
            return [
                _make_business(
                    place_id="A",
                    url_classification=UrlClassification.NONE,
                )
            ]

        def fake_discover(*args, **kwargs):
            called.append("discover")
            raise APIError(
                "Custom Search unavailable (HTTP 403): "
                "This project does not have the access to Custom Search JSON API."
            )

        def fake_reclassify(businesses):
            called.append("reclassify")
            return businesses

        def fake_audit(businesses, *args, **kwargs):
            called.append("audit")
            return businesses

        def fake_score(businesses):
            from leadscout.models import Lead, LeadTier

            called.append("score")
            for b in businesses:
                b.lead = Lead(tier=LeadTier.NO_WEBSITE, score=80, reasons=[])
            return businesses

        monkeypatch.setattr("leadscout.cli.search_places", fake_search)
        monkeypatch.setattr("leadscout.cli.discover_urls", fake_discover)
        monkeypatch.setattr("leadscout.cli.reclassify_urls", fake_reclassify)
        monkeypatch.setattr("leadscout.cli.audit_websites", fake_audit)
        monkeypatch.setattr("leadscout.cli.score_leads", fake_score)

        result = runner.invoke(
            cli,
            [
                "--data-dir", str(tmp_path), "run",
                "--location", "Test Town, ST",
            ],
        )

        assert result.exit_code == 0, result.output
        # discover was attempted, raised, then reclassify ran as fallback.
        assert called == ["search", "discover", "reclassify", "audit", "score"]
        assert "Custom Search unavailable" in result.output
        assert "Falling back" in result.output

    def test_run_force_flag_propagates_to_stages(
        self, runner, monkeypatch, tmp_path
    ):
        # --force should reach both discover_urls and audit_websites.
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-key")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_API_KEY", "cs-key")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_CX", "cs-cx")

        captured: dict = {}

        def fake_search(location, radius, key, **kwargs):
            return [
                _make_business(
                    place_id="A",
                    url_classification=UrlClassification.NONE,
                )
            ]

        def fake_discover(businesses, api_key, cx, *, data_dir, force):
            captured["discover_force"] = force
            return businesses

        def fake_audit(businesses, api_key, *, force):
            captured["audit_force"] = force
            return businesses

        def fake_score(businesses):
            from leadscout.models import Lead, LeadTier

            for b in businesses:
                b.lead = Lead(
                    tier=LeadTier.NO_WEBSITE, score=80, reasons=[]
                )
            return businesses

        monkeypatch.setattr("leadscout.cli.search_places", fake_search)
        monkeypatch.setattr("leadscout.cli.discover_urls", fake_discover)
        monkeypatch.setattr("leadscout.cli.audit_websites", fake_audit)
        monkeypatch.setattr("leadscout.cli.score_leads", fake_score)

        result = runner.invoke(
            cli,
            [
                "--data-dir", str(tmp_path), "run",
                "--location", "Town, ST", "--force",
            ],
        )

        assert result.exit_code == 0, result.output
        assert captured["discover_force"] is True
        assert captured["audit_force"] is True

    def test_run_export_csv_writes_file(
        self, runner, monkeypatch, tmp_path
    ):
        # --export csv at the end of `run` should produce the same
        # leads_<stem>_<date>.csv file the standalone `score` command does.
        monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-key")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_API_KEY", "cs-key")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_CX", "cs-cx")

        def fake_search(location, radius, key, **kwargs):
            return [
                _make_business(
                    place_id="A",
                    name="NoSiteBiz",
                    url_classification=UrlClassification.NONE,
                    rating=4.6,
                )
            ]

        def fake_discover(businesses, *args, **kwargs):
            return businesses

        def fake_audit(businesses, *args, **kwargs):
            return businesses

        def fake_score(businesses):
            from leadscout.models import Lead, LeadTier

            for b in businesses:
                b.lead = Lead(
                    tier=LeadTier.NO_WEBSITE, score=100,
                    reasons=["No website found"],
                )
            return businesses

        monkeypatch.setattr("leadscout.cli.search_places", fake_search)
        monkeypatch.setattr("leadscout.cli.discover_urls", fake_discover)
        monkeypatch.setattr("leadscout.cli.audit_websites", fake_audit)
        monkeypatch.setattr("leadscout.cli.score_leads", fake_score)

        result = runner.invoke(
            cli,
            [
                "--data-dir", str(tmp_path), "run",
                "--location", "Test Town, ST", "--export", "csv",
            ],
        )

        assert result.exit_code == 0, result.output
        # CSV should be written next to the data file with today's UTC date.
        csv_files = list(tmp_path.glob("leads_*.csv"))
        assert len(csv_files) == 1
        assert "Exported leads to" in result.output
