"""Tests for src/leadscout/search.py.

Strategy:
- Use httpx.MockTransport to stub the HTTP layer. We pass a `transport=`
  kwarg to create_client (which forwards it to httpx.Client) so the real
  retry/parse code paths run end-to-end with no network access.
- Disable real sleeping in tests via a `no_sleep` fixture. We patch
  `time.sleep` globally, which catches both our own pagination delays
  and tenacity's wait_exponential between retries.
- Test the small parsing helper (_parse_place) directly for the simple
  cases, and the higher-level _search_nearby for pagination + retry behavior.
"""

import json
from pathlib import Path

import httpx
import pytest

from leadscout.api import create_client
from leadscout.exceptions import APIError
from leadscout.models import UrlClassification, UrlSource
from leadscout.search import (
    _parse_place,
    _search_nearby,
    search_places,
)

# Fixtures live alongside the tests. Path(__file__).parent gives us this
# test file's directory regardless of where pytest is invoked from.
FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    """Read and parse a JSON fixture file."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace time.sleep with a no-op for the duration of a test.

    Both our own pagination delays and tenacity's exponential backoff
    waits ultimately call time.sleep, so this single patch makes retry
    tests run instantly instead of taking ~6 seconds.
    """
    # setattr on the time module replaces the real sleep function. Because
    # search.py does `import time` (not `from time import sleep`), it
    # looks up `time.sleep` at call time and gets our stub.
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


def _client_with_handler(handler):
    """Build an httpx.Client whose requests are routed to a callable handler.

    httpx.MockTransport hands each outgoing request to the handler, which
    returns an httpx.Response. This is the recommended way to test httpx
    code without touching the network.
    """
    transport = httpx.MockTransport(handler)
    return create_client(transport=transport)


# --- _parse_place ---


class TestParsePlace:
    def test_with_website_classifies_as_official_site(self):
        # Minimal payload covering every field _parse_place reads.
        place = {
            "id": "abc",
            "displayName": {"text": "Burger Joint", "languageCode": "en"},
            "formattedAddress": "123 Main St",
            "nationalPhoneNumber": "555-1111",
            "websiteUri": "https://burger.example",
            "rating": 4.2,
            "types": ["fast_food_restaurant", "restaurant"],
        }
        b = _parse_place(place)
        assert b.place_id == "abc"
        assert b.name == "Burger Joint"
        assert b.address == "123 Main St"
        assert b.phone == "555-1111"
        assert b.website == "https://burger.example"
        assert b.rating == 4.2
        assert b.business_type == "fast_food_restaurant"
        # Initial classification: any website -> assumed official.
        # Discovery (feature 03) reclassifies non-restaurant URLs.
        assert b.url_source == UrlSource.GOOGLE_PLACES
        assert b.url_classification == UrlClassification.OFFICIAL_SITE
        # last_scanned should be a tz-aware UTC datetime stamped on parse.
        assert b.last_scanned is not None
        assert b.last_scanned.tzinfo is not None

    def test_without_website_classifies_as_none(self):
        # Same shape minus websiteUri / nationalPhoneNumber.
        place = {
            "id": "xyz",
            "displayName": {"text": "Family Diner", "languageCode": "en"},
            "formattedAddress": "456 Oak Ave",
            "rating": 4.5,
            "types": ["restaurant"],
        }
        b = _parse_place(place)
        assert b.website == ""
        assert b.phone == ""
        assert b.url_source == UrlSource.NONE
        assert b.url_classification == UrlClassification.NONE

    def test_missing_optional_fields_falls_back_to_defaults(self):
        # Empty/missing fields shouldn't crash; defaults match Business defaults.
        place = {"id": "minimal"}
        b = _parse_place(place)
        assert b.place_id == "minimal"
        assert b.name == ""
        assert b.address == ""
        assert b.rating is None
        assert b.business_type == ""


# --- _search_nearby (single page) ---


class TestSearchNearbySinglePage:
    def test_three_businesses_two_with_websites(self, no_sleep):
        fixture = _load_fixture("places_nearby_response.json")

        def handler(_request):
            # Single-page response: no nextPageToken, so the loop exits
            # after one iteration.
            return httpx.Response(200, json=fixture)

        with _client_with_handler(handler) as client:
            results = _search_nearby(client, 30.4, -86.0, 5000, "fake_key", ["restaurant"])

        assert len(results) == 3
        with_site = [b for b in results if b.website]
        without = [b for b in results if not b.website]
        # The fixture is constructed so 2 of 3 have websites.
        assert len(with_site) == 2
        assert len(without) == 1
        assert all(b.url_source == UrlSource.GOOGLE_PLACES for b in with_site)
        assert all(b.url_source == UrlSource.NONE for b in without)


# --- _search_nearby (pagination) ---


class TestSearchNearbyPagination:
    def test_walks_next_page_token_until_exhausted(self, no_sleep):
        # Page 1 returns one place + a token. Page 2 returns one more
        # place with no token, ending the loop.
        page1 = {
            "places": [
                {
                    "id": "a",
                    "displayName": {"text": "First", "languageCode": "en"},
                    "formattedAddress": "1 St",
                    "types": ["restaurant"],
                }
            ],
            "nextPageToken": "tok-abc",
        }
        page2 = {
            "places": [
                {
                    "id": "b",
                    "displayName": {"text": "Second", "languageCode": "en"},
                    "formattedAddress": "2 St",
                    "types": ["restaurant"],
                }
            ],
        }
        # iter() with next() inside the handler gives us a deterministic
        # response sequence without sharing mutable state across tests.
        responses = iter([page1, page2])

        def handler(_request):
            return httpx.Response(200, json=next(responses))

        with _client_with_handler(handler) as client:
            results = _search_nearby(client, 30.4, -86.0, 5000, "fake_key", ["restaurant"])

        assert len(results) == 2
        # Set comparison because order across pages isn't part of the contract.
        assert {b.place_id for b in results} == {"a", "b"}


# --- _search_nearby (429 retry behavior) ---


class TestSearchNearbyRetry:
    def test_429_retries_then_succeeds(self, no_sleep):
        """Two 429s followed by a 200 should result in success after retries.

        RETRY_COUNT defaults to 3, so 2 failures + 1 success = 3 attempts,
        right at the edge of the retry budget.
        """
        fixture = _load_fixture("places_nearby_response.json")
        # Mutable counter shared with the closure handler. Using a dict
        # because integers are immutable in Python and a closed-over int
        # can't be reassigned without `nonlocal`.
        call_count = {"n": 0}

        def handler(_request):
            call_count["n"] += 1
            # Fail the first two calls with 429, then succeed on the third.
            if call_count["n"] < 3:
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(200, json=fixture)

        with _client_with_handler(handler) as client:
            results = _search_nearby(client, 30.4, -86.0, 5000, "fake_key", ["restaurant"])

        assert len(results) == 3
        assert call_count["n"] == 3

    def test_429_exhausts_retries_raises_apierror(self, no_sleep):
        """Persistent 429s should ultimately raise APIError, not RetryError."""

        def handler(_request):
            return httpx.Response(429, json={"error": "rate limited"})

        with _client_with_handler(handler) as client:
            with pytest.raises(APIError, match="quota exceeded"):
                _search_nearby(client, 30.4, -86.0, 5000, "fake_key", ["restaurant"])

    def test_401_does_not_retry(self, no_sleep):
        """Auth failures are permanent; tenacity should give up after 1 try."""
        call_count = {"n": 0}

        def handler(_request):
            call_count["n"] += 1
            return httpx.Response(401, json={"error": "bad key"})

        with _client_with_handler(handler) as client:
            with pytest.raises(APIError, match="auth failure"):
                _search_nearby(client, 30.4, -86.0, 5000, "fake_key", ["restaurant"])

        # 4xx auth errors are not in RETRYABLE_STATUS_CODES, so the
        # decorator should re-raise immediately on the first attempt.
        assert call_count["n"] == 1


# --- search_places (geocode + nearby integration) ---


class TestSearchPlacesIntegration:
    def test_happy_path_geocodes_then_searches(self, no_sleep, monkeypatch):
        """End-to-end: geocode call followed by nearby search call."""
        fixture = _load_fixture("places_nearby_response.json")

        # Two-stage handler: first request goes to the Geocoding API,
        # subsequent requests to the Places Nearby endpoint.
        def handler(request):
            url = str(request.url)
            if "maps.googleapis.com/maps/api/geocode" in url:
                return httpx.Response(
                    200,
                    json={
                        "status": "OK",
                        "results": [
                            {
                                "geometry": {
                                    "location": {"lat": 30.4, "lng": -86.0}
                                }
                            }
                        ],
                    },
                )
            return httpx.Response(200, json=fixture)

        # Patch create_client used inside search_places so it uses our mock
        # transport. We import the module by string and replace its
        # `create_client` attribute; the original is restored automatically
        # by monkeypatch teardown.
        def fake_create_client(**kwargs):
            return _client_with_handler(handler)

        monkeypatch.setattr("leadscout.search.create_client", fake_create_client)

        results = search_places("Santa Rosa Beach, FL", 5000, "fake_key")
        assert len(results) == 3

    def test_zero_results_geocode_raises_apierror(self, no_sleep, monkeypatch):
        def handler(_request):
            return httpx.Response(
                200,
                json={"status": "ZERO_RESULTS", "results": []},
            )

        def fake_create_client(**kwargs):
            return _client_with_handler(handler)

        monkeypatch.setattr("leadscout.search.create_client", fake_create_client)

        with pytest.raises(APIError, match="no results"):
            search_places("nowhere actual", 5000, "fake_key")
