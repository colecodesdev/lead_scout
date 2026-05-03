"""Tests for src/leadscout/audit.py.

Strategy mirrors prior features: mock at the boundary, real code inside.
- PSI HTTP layer: httpx.MockTransport (consistent with test_search.py /
  test_discovery.py).
- Pure functions (_parse_psi_scores, _compute_deficiencies, _is_eligible):
  tested directly with synthetic inputs.
- Playwright DOM layer: real headless Chromium against local HTML files
  via file:// URLs. These tests skip cleanly if `playwright install
  chromium` hasn't been run (so the suite still runs in fresh checkouts).
- audit_websites end-to-end: monkeypatch _run_dom_checks + _fetch_psi_scores
  so we exercise the orchestration logic (eligibility filter, mutation,
  deficiency compute) without launching a browser.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from leadscout.api import create_client
from leadscout.audit import (
    _compute_deficiencies,
    _fetch_psi_scores,
    _is_eligible,
    _parse_psi_scores,
    audit_websites,
)
from leadscout.exceptions import APIError, AuditError
from leadscout.models import Audit, Business, UrlClassification, UrlSource

FIXTURES = Path(__file__).parent / "fixtures"
HTML_FIXTURES = FIXTURES / "audit_html"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def no_sleep(monkeypatch):
    """Patch time.sleep so PSI rate-limit gaps + tenacity retries are fast."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


def _make_business(**overrides) -> Business:
    """Default to an audit-eligible business (official_site URL)."""
    defaults = {
        "place_id": "test_id",
        "name": "Coastal Catch",
        "address": "100 Beach Rd, Santa Rosa Beach, FL 32459, USA",
        "website": "https://coastalcatch.example.com",
        "url_source": UrlSource.GOOGLE_PLACES,
        "url_classification": UrlClassification.OFFICIAL_SITE,
    }
    defaults.update(overrides)
    return Business(**defaults)


# ---------------------------------------------------------------------------
# _parse_psi_scores
# ---------------------------------------------------------------------------


class TestParsePsiScores:
    def test_extracts_all_four_categories(self):
        data = _load_fixture("pagespeed_response.json")
        result = _parse_psi_scores(data)
        # Scores in the API are 0-1; we round to 0-100 for display.
        assert result == {
            "performance": 87,
            "accessibility": 95,
            "seo": 92,
            "best_practices": 83,
        }

    def test_handles_poor_scores(self):
        data = _load_fixture("pagespeed_poor_scores.json")
        result = _parse_psi_scores(data)
        assert result["performance"] == 23
        assert result["best_practices"] == 42

    def test_missing_categories_become_none(self):
        # PSI sometimes can't evaluate a category for a given page;
        # the JSON includes a null score in that case.
        data = {
            "lighthouseResult": {
                "categories": {
                    "performance": {"score": None},
                    "accessibility": {"score": 0.9},
                }
            }
        }
        result = _parse_psi_scores(data)
        assert result["performance"] is None
        assert result["accessibility"] == 90
        # Categories absent from the response come out as None too.
        assert result["seo"] is None
        assert result["best_practices"] is None

    def test_empty_response_all_none(self):
        # Defensive: a malformed PSI response shouldn't crash; every
        # category just becomes None.
        result = _parse_psi_scores({})
        assert all(v is None for v in result.values())


# ---------------------------------------------------------------------------
# _fetch_psi_scores (HTTP integration via MockTransport)
# ---------------------------------------------------------------------------


class TestFetchPsiScores:
    def test_happy_path(self, no_sleep):
        fixture = _load_fixture("pagespeed_response.json")
        captured_params: dict = {}

        def handler(request):
            # Verify the request has the expected query params.
            captured_params.update(dict(request.url.params))
            return httpx.Response(200, json=fixture)

        transport = httpx.MockTransport(handler)
        with create_client(transport=transport) as client:
            result = _fetch_psi_scores(
                client, "https://example.com", "mobile", "fakekey"
            )

        assert result["performance"] == 87
        assert captured_params["url"] == "https://example.com"
        assert captured_params["strategy"] == "mobile"
        assert captured_params["key"] == "fakekey"

    def test_unauthenticated_when_no_key(self, no_sleep):
        # Spec says unauthenticated PSI works at lower quota. Verify
        # we don't attach an empty `key` param when api_key is None.
        captured_params: dict = {}

        def handler(request):
            captured_params.update(dict(request.url.params))
            return httpx.Response(
                200, json={"lighthouseResult": {"categories": {}}}
            )

        transport = httpx.MockTransport(handler)
        with create_client(transport=transport) as client:
            _fetch_psi_scores(client, "https://example.com", "mobile", None)

        assert "key" not in captured_params

    def test_429_exhausted_raises_apierror(self, no_sleep):
        def handler(_request):
            return httpx.Response(429)

        transport = httpx.MockTransport(handler)
        with create_client(transport=transport) as client:
            with pytest.raises(APIError, match="HTTP 429"):
                _fetch_psi_scores(
                    client, "https://example.com", "mobile", None
                )


# ---------------------------------------------------------------------------
# _compute_deficiencies
# ---------------------------------------------------------------------------


class TestComputeDeficiencies:
    def test_clean_audit_has_no_deficiencies(self):
        # Site that passes every check shouldn't produce deficiencies.
        audit = Audit(
            lighthouse_mobile={"performance": 90, "accessibility": 90,
                               "seo": 90, "best_practices": 90},
            has_menu=True, has_hours=True, has_contact_info=True,
            has_mobile_viewport=True, has_ssl=True,
            has_online_ordering=True, has_reservation=True,
            broken_assets=[],
        )
        assert _compute_deficiencies(audit) == []

    def test_no_ssl_produces_deficiency(self):
        audit = Audit(has_ssl=False, has_menu=True, has_hours=True,
                      has_contact_info=True, has_mobile_viewport=True,
                      has_online_ordering=True, has_reservation=True)
        result = _compute_deficiencies(audit)
        assert "No SSL certificate" in result

    def test_low_mobile_performance_produces_deficiency_with_score(self):
        audit = Audit(
            lighthouse_mobile={"performance": 23, "accessibility": 90,
                               "seo": 90, "best_practices": 90},
            has_menu=True, has_hours=True, has_contact_info=True,
            has_mobile_viewport=True, has_ssl=True,
            has_online_ordering=True, has_reservation=True,
        )
        result = _compute_deficiencies(audit)
        # Stable string: "Mobile performance score: <N>/100" for the
        # scoring step (feature 05) to match on.
        assert "Mobile performance score: 23/100" in result

    def test_high_mobile_performance_no_deficiency(self):
        audit = Audit(
            lighthouse_mobile={"performance": 80, "accessibility": 90,
                               "seo": 90, "best_practices": 90},
            has_menu=True, has_hours=True, has_contact_info=True,
            has_mobile_viewport=True, has_ssl=True,
            has_online_ordering=True, has_reservation=True,
        )
        result = _compute_deficiencies(audit)
        # No "Mobile performance score" line: 80 is above the threshold.
        assert not any("performance score" in d for d in result)

    def test_broken_assets_grouped_by_type(self):
        audit = Audit(
            has_menu=True, has_hours=True, has_contact_info=True,
            has_mobile_viewport=True, has_ssl=True,
            has_online_ordering=True, has_reservation=True,
            broken_assets=[
                {"url": "a.png", "status": 404, "type": "image"},
                {"url": "b.png", "status": 404, "type": "image"},
                {"url": "c.css", "status": 500, "type": "stylesheet"},
            ],
        )
        result = _compute_deficiencies(audit)
        assert "2 broken images" in result
        assert "1 broken stylesheets" in result

    def test_missing_essentials_listed(self):
        # Empty Audit: every check is False / None / [].
        audit = Audit()
        result = _compute_deficiencies(audit)
        # Should hit each of the essential checks since all defaults are False.
        for expected in [
            "No SSL certificate",
            "No mobile viewport configured",
            "No menu page found",
            "No business hours found",
            "No phone or contact info found",
            "No online ordering",
            "No reservation system",
        ]:
            assert expected in result


# ---------------------------------------------------------------------------
# _is_eligible
# ---------------------------------------------------------------------------


class TestIsEligible:
    def test_official_site_with_no_audit_is_eligible(self):
        biz = _make_business()
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert _is_eligible(biz, cutoff, force=False) is True

    def test_social_media_classification_is_not_eligible(self):
        biz = _make_business(
            url_classification=UrlClassification.SOCIAL_MEDIA
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert _is_eligible(biz, cutoff, force=False) is False

    def test_directory_listing_is_not_eligible(self):
        biz = _make_business(
            url_classification=UrlClassification.DIRECTORY_LISTING
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert _is_eligible(biz, cutoff, force=False) is False

    def test_fresh_audit_skipped_without_force(self):
        # Audit from yesterday is fresher than 7-day cutoff -> skip.
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        biz = _make_business(audit=Audit(audited_at=recent))
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert _is_eligible(biz, cutoff, force=False) is False

    def test_fresh_audit_re_audited_with_force(self):
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        biz = _make_business(audit=Audit(audited_at=recent))
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert _is_eligible(biz, cutoff, force=True) is True

    def test_stale_audit_re_audited_without_force(self):
        # Audit from 30 days ago is past the 7-day window -> re-audit.
        stale = datetime.now(timezone.utc) - timedelta(days=30)
        biz = _make_business(audit=Audit(audited_at=stale))
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert _is_eligible(biz, cutoff, force=False) is True


# ---------------------------------------------------------------------------
# audit_websites orchestration (with both layers stubbed out)
# ---------------------------------------------------------------------------


class TestAuditWebsitesOrchestration:
    def test_only_official_sites_audited(self, no_sleep, monkeypatch):
        # Track which businesses got their _audit_one called.
        audited_names: list[str] = []

        def fake_audit_one(biz, *_args, **_kwargs):
            audited_names.append(biz.name)
            biz.audit = Audit(audited_at=datetime.now(timezone.utc))

        monkeypatch.setattr("leadscout.audit._audit_one", fake_audit_one)
        # Stub out playwright entirely so no browser is launched.
        monkeypatch.setattr(
            "playwright.sync_api.sync_playwright",
            lambda: _NoOpPlaywrightContext(),
        )

        bizs = [
            _make_business(name="OfficialA"),
            _make_business(
                name="SocialB",
                url_classification=UrlClassification.SOCIAL_MEDIA,
            ),
            _make_business(
                name="DirectoryC",
                url_classification=UrlClassification.DIRECTORY_LISTING,
            ),
            _make_business(name="OfficialD"),
        ]
        audit_websites(bizs)

        assert sorted(audited_names) == ["OfficialA", "OfficialD"]
        # Only the audited businesses got an Audit attached.
        assert bizs[0].audit is not None
        assert bizs[1].audit is None
        assert bizs[2].audit is None
        assert bizs[3].audit is not None

    def test_force_overrides_freshness_skip(self, no_sleep, monkeypatch):
        called = {"n": 0}

        def fake_audit_one(*_args, **_kwargs):
            called["n"] += 1

        monkeypatch.setattr("leadscout.audit._audit_one", fake_audit_one)
        monkeypatch.setattr(
            "playwright.sync_api.sync_playwright",
            lambda: _NoOpPlaywrightContext(),
        )

        recent = datetime.now(timezone.utc) - timedelta(days=1)
        biz = _make_business(audit=Audit(audited_at=recent))

        # Without force: skipped, no _audit_one call.
        audit_websites([biz])
        assert called["n"] == 0

        # With force: audited.
        audit_websites([biz], force=True)
        assert called["n"] == 1

    def test_no_eligible_businesses_does_not_launch_browser(
        self, no_sleep, monkeypatch
    ):
        # If nothing's eligible, sync_playwright should never be called.
        # If we accidentally launched a real browser when not needed,
        # the test would slow down dramatically.
        sentinel = {"launched": False}

        def fake_sync_playwright():
            sentinel["launched"] = True
            return _NoOpPlaywrightContext()

        monkeypatch.setattr(
            "playwright.sync_api.sync_playwright", fake_sync_playwright
        )

        biz = _make_business(
            url_classification=UrlClassification.SOCIAL_MEDIA
        )
        audit_websites([biz])

        assert sentinel["launched"] is False


class _NoOpPlaywrightContext:
    """Minimal stand-in for sync_playwright()'s context manager.

    Used by orchestration tests where _audit_one is also stubbed; the
    browser is never touched, but sync_playwright()'s `with` protocol
    must work for audit_websites' `with sync_playwright() as pw:` block.
    """

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @property
    def chromium(self):
        return self

    def launch(self, **_kwargs):
        return self

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Real Playwright DOM checks against local HTML
# ---------------------------------------------------------------------------


# Lazy import + skip-if-missing so the rest of the suite runs even without
# `playwright install chromium`. The browser binary is a separate download
# from the pip package.
def _playwright_or_skip():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as e:
        pw.stop()
        pytest.skip(f"chromium browser not installed: {e}")
    return pw, browser


@pytest.fixture
def real_browser():
    """Yield a real headless Chromium browser; skip the test if unavailable."""
    pw, browser = _playwright_or_skip()
    try:
        yield browser
    finally:
        browser.close()
        pw.stop()


def _file_url(path: Path) -> str:
    """Build a file:// URL for a local fixture HTML file."""
    return path.resolve().as_uri()


class TestRunDomChecksReal:
    def test_full_html_passes_all_checks(self, real_browser):
        from leadscout.audit import _run_dom_checks

        url = _file_url(HTML_FIXTURES / "full.html")
        result = _run_dom_checks(real_browser, url)

        assert result["has_menu"] is True
        assert result["has_hours"] is True
        assert result["has_contact_info"] is True
        assert result["has_mobile_viewport"] is True
        # file:// URLs are not https://
        assert result["has_ssl"] is False
        assert result["has_online_ordering"] is True
        assert result["has_reservation"] is True
        # Phone in fixture: (850) 555-0123. Pattern should match.
        assert result["phone"] == "(850) 555-0123"
        assert result["load_time_seconds"] is not None
        assert result["load_time_seconds"] > 0

    def test_empty_html_fails_all_checks(self, real_browser):
        from leadscout.audit import _run_dom_checks

        url = _file_url(HTML_FIXTURES / "empty.html")
        result = _run_dom_checks(real_browser, url)

        assert result["has_menu"] is False
        # "empty.html" has no hours-pattern words.
        assert result["has_hours"] is False
        assert result["has_contact_info"] is False
        assert result["has_mobile_viewport"] is False
        assert result["has_online_ordering"] is False
        assert result["has_reservation"] is False
        assert result["phone"] is None

    def test_menu_detected_via_href_only(self, real_browser):
        # Spec gotcha: SPA sites may put the menu link on an icon-only
        # anchor (no visible "menu" word) with href="/menu". The text
        # check finds nothing, the href fallback in _check_menu must.
        from leadscout.audit import _run_dom_checks

        url = _file_url(HTML_FIXTURES / "menu_in_href.html")
        result = _run_dom_checks(real_browser, url)

        # No visible "menu" text on this page; only signal is the href.
        assert result["has_menu"] is True


# ---------------------------------------------------------------------------
# audit_websites end-to-end with stubbed layers
# ---------------------------------------------------------------------------


class TestAuditWebsitesEndToEnd:
    def test_phone_backfill_from_dom(self, no_sleep, monkeypatch):
        # The DOM scrape returns a phone number; Business.phone was empty;
        # the audit should backfill it.
        def fake_dom(_browser, _url):
            return {
                "has_menu": True, "has_hours": True, "has_contact_info": True,
                "has_mobile_viewport": True, "has_ssl": True,
                "has_online_ordering": True, "has_reservation": True,
                "load_time_seconds": 1.5, "broken_assets": [],
                "phone": "(850) 555-0123",
            }

        def fake_psi(_client, _url, _strategy, _key):
            return {"performance": 90, "accessibility": 90,
                    "seo": 90, "best_practices": 90}

        monkeypatch.setattr("leadscout.audit._run_dom_checks", fake_dom)
        monkeypatch.setattr("leadscout.audit._fetch_psi_scores", fake_psi)
        monkeypatch.setattr(
            "playwright.sync_api.sync_playwright",
            lambda: _NoOpPlaywrightContext(),
        )

        biz = _make_business(phone="")
        audit_websites([biz])
        assert biz.phone == "(850) 555-0123"

    def test_phone_not_overwritten_when_already_present(
        self, no_sleep, monkeypatch
    ):
        def fake_dom(_browser, _url):
            return {
                "has_menu": True, "has_hours": True, "has_contact_info": True,
                "has_mobile_viewport": True, "has_ssl": True,
                "has_online_ordering": True, "has_reservation": True,
                "load_time_seconds": 1.5, "broken_assets": [],
                "phone": "(850) 555-0123",
            }

        monkeypatch.setattr("leadscout.audit._run_dom_checks", fake_dom)
        monkeypatch.setattr(
            "leadscout.audit._fetch_psi_scores",
            lambda *a, **kw: {"performance": 90, "accessibility": 90,
                              "seo": 90, "best_practices": 90},
        )
        monkeypatch.setattr(
            "playwright.sync_api.sync_playwright",
            lambda: _NoOpPlaywrightContext(),
        )

        # Existing phone is preserved (Places said "555-0001"; DOM says
        # "(850) 555-0123"; we don't overwrite the upstream truth).
        biz = _make_business(phone="555-0001")
        audit_websites([biz])
        assert biz.phone == "555-0001"

    def test_dom_failure_still_records_partial_audit(
        self, no_sleep, monkeypatch
    ):
        # When Cloudflare blocks the DOM check, the audit should still
        # record PSI scores and the audited_at timestamp. All DOM bools
        # default to False and deficiencies reflect the failure.
        def boom(_browser, _url):
            raise AuditError("site blocked automated access (HTTP 403)")

        monkeypatch.setattr("leadscout.audit._run_dom_checks", boom)
        monkeypatch.setattr(
            "leadscout.audit._fetch_psi_scores",
            lambda *a, **kw: {"performance": 80, "accessibility": 90,
                              "seo": 80, "best_practices": 80},
        )
        monkeypatch.setattr(
            "playwright.sync_api.sync_playwright",
            lambda: _NoOpPlaywrightContext(),
        )

        biz = _make_business()
        audit_websites([biz])

        # PSI scores still captured.
        assert biz.audit is not None
        assert biz.audit.lighthouse_mobile == {
            "performance": 80, "accessibility": 90,
            "seo": 80, "best_practices": 80,
        }
        # All DOM bools at default (False).
        assert biz.audit.has_menu is False
        assert biz.audit.has_ssl is False
        # Deficiencies populated (everything missing).
        assert "No SSL certificate" in biz.audit.deficiencies
        assert "No menu page found" in biz.audit.deficiencies
