"""Tests for src/leadscout/discovery.py.

Strategy:
- Address parsing, URL classification, and result matching are pure
  functions; tested directly.
- HTTP layer (Custom Search) tested with httpx.MockTransport, same
  pattern as test_search.py.
- QuotaTracker tested for: fresh start, same-day persistence, cross-day
  reset, corrupt-file defensive default, hard stop at SAFE_LIMIT.
- discover_urls integration: business with no URL gets discovered,
  business with social URL gets reclassified, --force re-runs already
  classified, quota exhaustion mid-loop skips the rest gracefully.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from leadscout.api import create_client
from leadscout.config import (
    CUSTOM_SEARCH_SAFE_LIMIT,
    CUSTOM_SEARCH_WARN_THRESHOLD,
)
from leadscout.discovery import (
    QUOTA_FILENAME,
    QuotaTracker,
    _build_query,
    _classify_url,
    _extract_city_state,
    _match_results,
    discover_urls,
)
from leadscout.exceptions import APIError
from leadscout.models import Business, UrlClassification, UrlSource

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def no_sleep(monkeypatch):
    """Skip tenacity backoff sleeps for fast retry tests."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


def _client_with_handler(handler):
    transport = httpx.MockTransport(handler)
    return create_client(transport=transport)


def _make_business(**overrides) -> Business:
    """Helper: a Business with sensible defaults, overridden as needed."""
    defaults = {
        "place_id": "test_id",
        "name": "Coastal Catch",
        "address": "100 Beach Rd, Santa Rosa Beach, FL 32459, USA",
        "website": "",
        "url_source": UrlSource.NONE,
        "url_classification": UrlClassification.NONE,
    }
    defaults.update(overrides)
    return Business(**defaults)


# ---------------------------------------------------------------------------
# _extract_city_state
# ---------------------------------------------------------------------------


class TestExtractCityState:
    def test_us_address_with_usa_suffix(self):
        result = _extract_city_state(
            "100 Beach Rd, Santa Rosa Beach, FL 32459, USA"
        )
        assert result == ("Santa Rosa Beach", "FL")

    def test_us_address_without_country_suffix(self):
        result = _extract_city_state("100 Beach Rd, Santa Rosa Beach, FL 32459")
        assert result == ("Santa Rosa Beach", "FL")

    def test_us_address_with_zip_plus_four(self):
        result = _extract_city_state(
            "100 Beach Rd, Santa Rosa Beach, FL 32459-1234, USA"
        )
        assert result == ("Santa Rosa Beach", "FL")

    def test_address_with_suite_number(self):
        # The lazy [^,]+? + anchored end means the rightmost
        # ", City, ST ZIP" wins regardless of leading "Suite" parts.
        result = _extract_city_state(
            "200 Gulf Pl, Suite 5, Destin, FL 32541, USA"
        )
        assert result == ("Destin", "FL")

    def test_empty_address_returns_none(self):
        assert _extract_city_state("") is None

    def test_unparseable_address_returns_none(self):
        # International / freeform address should fall through cleanly.
        assert _extract_city_state("123 Some Street, Toronto, ON M5V 3A8") is None
        assert _extract_city_state("just a single word") is None


# ---------------------------------------------------------------------------
# _classify_url
# ---------------------------------------------------------------------------


class TestClassifyUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.facebook.com/somebusiness",
            "https://m.facebook.com/biz",  # subdomain still matches
            "https://instagram.com/biz",
            "https://twitter.com/biz",
            "https://x.com/biz",
            "https://tiktok.com/@biz",
        ],
    )
    def test_social_media_domains_classify_correctly(self, url):
        assert _classify_url(url) == UrlClassification.SOCIAL_MEDIA

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.yelp.com/biz/burger-joint",
            "https://tripadvisor.com/Restaurant_Review-x",
            "https://grubhub.com/restaurant/x",
            "https://www.doordash.com/store/x",
            "https://ubereats.com/store/x",
            "https://opentable.com/r/x",
            "https://yellowpages.com/x",
        ],
    )
    def test_directory_domains_classify_correctly(self, url):
        assert _classify_url(url) == UrlClassification.DIRECTORY_LISTING

    @pytest.mark.parametrize(
        "url",
        [
            "https://burgerjoint.example.com",
            "https://www.burgerjoint.com/menu",
            "http://burgerjoint.com",
            "burgerjoint.com",  # no scheme; classifier must still cope
        ],
    )
    def test_other_domains_classify_as_official_site(self, url):
        assert _classify_url(url) == UrlClassification.OFFICIAL_SITE

    def test_empty_url_returns_none(self):
        assert _classify_url("") == UrlClassification.NONE


# ---------------------------------------------------------------------------
# _match_results
# ---------------------------------------------------------------------------


class TestMatchResults:
    def test_returns_official_site_match_when_present(self):
        # Fixture has 4 results; the first is the official site, then
        # Facebook, Yelp, and a roundup blog. The matcher should
        # promote the official site even though Facebook also matches.
        items = _load_fixture("custom_search_response.json")["items"]
        result = _match_results("Coastal Catch", items)
        assert result is not None
        url, classification = result
        assert "coastalcatch.example.com" in url
        assert classification == UrlClassification.OFFICIAL_SITE

    def test_falls_back_to_first_match_when_no_official_site(self):
        # Synthetic input where every match is social/directory.
        items = [
            {
                "title": "Coastal Catch | Yelp",
                "link": "https://www.yelp.com/biz/coastal-catch",
                "displayLink": "www.yelp.com",
            },
            {
                "title": "Coastal Catch - Facebook",
                "link": "https://www.facebook.com/coastalcatch",
                "displayLink": "www.facebook.com",
            },
        ]
        result = _match_results("Coastal Catch", items)
        assert result is not None
        url, classification = result
        # First above-threshold match wins when none is official.
        assert "yelp.com" in url
        assert classification == UrlClassification.DIRECTORY_LISTING

    def test_no_match_when_below_threshold(self):
        # Test 5 from the spec: completely unrelated results shouldn't match.
        items = _load_fixture("custom_search_no_match.json")["items"]
        result = _match_results("Bud & Alley's", items)
        assert result is None

    def test_fuzzy_match_handles_word_order_and_extras(self):
        # Test 4 from the spec: token_sort_ratio should still match
        # "Donut Hole Destin" against "The Donut Hole" above threshold.
        items = [
            {
                "title": "Donut Hole Destin - Best Donuts on 30A",
                "link": "https://thedonuthole.example.com",
                "displayLink": "thedonuthole.example.com",
            }
        ]
        result = _match_results("The Donut Hole", items)
        assert result is not None
        url, _ = result
        assert "thedonuthole.example.com" in url

    def test_empty_items_returns_none(self):
        assert _match_results("anything", []) is None

    def test_match_via_display_link_when_title_unrelated(self):
        # Real-world case: a result page has a generic title but the
        # business name is in the domain. The matcher considers both
        # title and displayLink, so the displayLink alone should be
        # enough to push us above threshold.
        items = [
            {
                "title": "Local Eats Roundup 2026",
                "link": "https://coastalcatch.example.com/about",
                "displayLink": "coastalcatch.example.com",
            }
        ]
        result = _match_results("Coastal Catch", items)
        assert result is not None
        url, classification = result
        assert "coastalcatch.example.com" in url
        assert classification == UrlClassification.OFFICIAL_SITE

    def test_skips_items_with_no_url(self):
        # Defensive: results missing both `link` and `displayLink` are
        # silently skipped so we don't crash or set website to "".
        # A second (valid) item should still produce a match.
        items = [
            # First item: passes name fuzzy match but has no URL fields.
            {"title": "Coastal Catch", "link": "", "displayLink": ""},
            # Second item: a valid match.
            {
                "title": "Coastal Catch | Fresh Seafood",
                "link": "https://coastalcatch.example.com",
                "displayLink": "coastalcatch.example.com",
            },
        ]
        result = _match_results("Coastal Catch", items)
        assert result is not None
        url, _ = result
        assert "coastalcatch.example.com" in url


# ---------------------------------------------------------------------------
# _build_query
# ---------------------------------------------------------------------------


class TestBuildQuery:
    def test_query_includes_quoted_name_and_location(self):
        biz = _make_business()
        query = _build_query(biz)
        assert '"Coastal Catch"' in query
        assert '"Santa Rosa Beach, FL"' in query

    def test_query_falls_back_to_name_only_for_unparseable_address(self):
        biz = _make_business(address="just a freeform address")
        query = _build_query(biz)
        assert query == '"Coastal Catch"'


# ---------------------------------------------------------------------------
# QuotaTracker
# ---------------------------------------------------------------------------


class TestQuotaTracker:
    def test_fresh_start_count_is_zero(self, tmp_path):
        tracker = QuotaTracker(tmp_path)
        assert tracker.count == 0
        assert tracker.remaining == CUSTOM_SEARCH_SAFE_LIMIT

    def test_consume_increments_and_persists(self, tmp_path):
        tracker = QuotaTracker(tmp_path)
        assert tracker.consume() is True
        assert tracker.count == 1
        # New tracker on the same path should pick up the persisted count.
        tracker2 = QuotaTracker(tmp_path)
        assert tracker2.count == 1

    def test_consume_returns_false_at_safe_limit(self, tmp_path):
        # Pre-seed the file so we don't actually loop SAFE_LIMIT times.
        # Persist a state file directly.
        seed = {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "count": CUSTOM_SEARCH_SAFE_LIMIT,
        }
        (tmp_path / QUOTA_FILENAME).write_text(json.dumps(seed))
        tracker = QuotaTracker(tmp_path)
        assert tracker.consume() is False
        assert tracker.remaining == 0

    def test_warn_threshold_logs_once(self, tmp_path, caplog):
        # Seed at one below the warn threshold so a single consume()
        # crosses it and triggers the warning.
        seed = {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "count": CUSTOM_SEARCH_WARN_THRESHOLD - 1,
        }
        (tmp_path / QUOTA_FILENAME).write_text(json.dumps(seed))
        tracker = QuotaTracker(tmp_path)
        with caplog.at_level("WARNING", logger="leadscout.discovery"):
            assert tracker.consume() is True
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("quota at" in r.getMessage() for r in warnings)

    def test_cross_day_reset(self, tmp_path):
        # Seed the file with yesterday's date and a high count. A new
        # tracker should observe the date mismatch and reset to 0.
        yesterday = (
            datetime.now(timezone.utc).date() - timedelta(days=1)
        ).isoformat()
        seed = {"date": yesterday, "count": 50}
        (tmp_path / QUOTA_FILENAME).write_text(json.dumps(seed))
        tracker = QuotaTracker(tmp_path)
        assert tracker.count == 0

    def test_corrupt_file_refuses_queries(self, tmp_path):
        # Defensive default: an unparseable file means we set count to
        # SAFE_LIMIT so consume() returns False, never overrunning the
        # cap because of a corrupt counter.
        (tmp_path / QUOTA_FILENAME).write_text("{not valid json")
        tracker = QuotaTracker(tmp_path)
        assert tracker.count == CUSTOM_SEARCH_SAFE_LIMIT
        assert tracker.consume() is False

    def test_corrupt_file_missing_keys_refuses_queries(self, tmp_path):
        (tmp_path / QUOTA_FILENAME).write_text(json.dumps({"random": "data"}))
        tracker = QuotaTracker(tmp_path)
        assert tracker.consume() is False


# ---------------------------------------------------------------------------
# discover_urls integration
# ---------------------------------------------------------------------------


class TestDiscoverUrls:
    def test_discovers_url_for_business_with_none_source(
        self, no_sleep, monkeypatch, tmp_path
    ):
        fixture = _load_fixture("custom_search_response.json")

        def handler(_request):
            return httpx.Response(200, json=fixture)

        monkeypatch.setattr(
            "leadscout.discovery.create_client",
            lambda **kwargs: _client_with_handler(handler),
        )

        biz = _make_business()
        result = discover_urls([biz], "key", "cx", data_dir=tmp_path)

        assert len(result) == 1
        assert result[0].url_source == UrlSource.SEARCH_DISCOVERED
        assert "coastalcatch.example.com" in result[0].website
        assert result[0].url_classification == UrlClassification.OFFICIAL_SITE

    def test_skips_business_with_existing_url_unless_force(
        self, no_sleep, monkeypatch, tmp_path
    ):
        # If discover is called without force on a business that already
        # has a URL, it should NOT issue a search; it should only run the
        # reclassification step.
        call_count = {"n": 0}

        def handler(_request):
            call_count["n"] += 1
            return httpx.Response(200, json={"items": []})

        monkeypatch.setattr(
            "leadscout.discovery.create_client",
            lambda **kwargs: _client_with_handler(handler),
        )

        biz = _make_business(
            website="https://www.facebook.com/coastalcatch",
            url_source=UrlSource.GOOGLE_PLACES,
            url_classification=UrlClassification.OFFICIAL_SITE,
        )
        result = discover_urls([biz], "key", "cx", data_dir=tmp_path)

        # No HTTP call should have been issued.
        assert call_count["n"] == 0
        # But the classification should be corrected: a facebook URL
        # mistakenly classified as official_site by feature 02 should
        # become social_media after this step.
        assert result[0].url_classification == UrlClassification.SOCIAL_MEDIA
        # url_source unchanged because we didn't run a discovery search.
        assert result[0].url_source == UrlSource.GOOGLE_PLACES

    def test_force_reruns_search_for_classified_business(
        self, no_sleep, monkeypatch, tmp_path
    ):
        fixture = _load_fixture("custom_search_response.json")
        call_count = {"n": 0}

        def handler(_request):
            call_count["n"] += 1
            return httpx.Response(200, json=fixture)

        monkeypatch.setattr(
            "leadscout.discovery.create_client",
            lambda **kwargs: _client_with_handler(handler),
        )

        biz = _make_business(
            website="https://stale.example.com",
            url_source=UrlSource.GOOGLE_PLACES,
            url_classification=UrlClassification.OFFICIAL_SITE,
        )
        result = discover_urls(
            [biz], "key", "cx", data_dir=tmp_path, force=True
        )

        assert call_count["n"] == 1
        # Force overwrote the existing URL with the discovered one.
        assert "coastalcatch.example.com" in result[0].website
        assert result[0].url_source == UrlSource.SEARCH_DISCOVERED

    def test_no_match_leaves_business_unchanged(
        self, no_sleep, monkeypatch, tmp_path
    ):
        fixture = _load_fixture("custom_search_no_match.json")

        def handler(_request):
            return httpx.Response(200, json=fixture)

        monkeypatch.setattr(
            "leadscout.discovery.create_client",
            lambda **kwargs: _client_with_handler(handler),
        )

        biz = _make_business(name="Bud & Alley's")
        result = discover_urls([biz], "key", "cx", data_dir=tmp_path)
        assert result[0].url_source == UrlSource.NONE
        assert result[0].url_classification == UrlClassification.NONE
        assert result[0].website == ""

    def test_quota_exhaustion_skips_remaining_businesses(
        self, no_sleep, monkeypatch, tmp_path
    ):
        # Pre-seed the quota file at the safe limit so the first
        # consume() call fails. No HTTP requests should be issued.
        seed = {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "count": CUSTOM_SEARCH_SAFE_LIMIT,
        }
        (tmp_path / QUOTA_FILENAME).write_text(json.dumps(seed))

        call_count = {"n": 0}

        def handler(_request):
            call_count["n"] += 1
            return httpx.Response(200, json={"items": []})

        monkeypatch.setattr(
            "leadscout.discovery.create_client",
            lambda **kwargs: _client_with_handler(handler),
        )

        bizs = [_make_business(place_id="a"), _make_business(place_id="b")]
        result = discover_urls(bizs, "key", "cx", data_dir=tmp_path)

        # Neither business issued a search request.
        assert call_count["n"] == 0
        # Both stay in their original state.
        for b in result:
            assert b.url_source == UrlSource.NONE

    def test_401_surfaces_as_apierror(self, no_sleep, monkeypatch, tmp_path):
        def handler(_request):
            return httpx.Response(401, json={"error": "bad key"})

        monkeypatch.setattr(
            "leadscout.discovery.create_client",
            lambda **kwargs: _client_with_handler(handler),
        )

        biz = _make_business()
        with pytest.raises(APIError, match="auth failure"):
            discover_urls([biz], "key", "cx", data_dir=tmp_path)
