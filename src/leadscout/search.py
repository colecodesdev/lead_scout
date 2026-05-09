"""Google Places (New) integration: discover businesses in a target area.

This is the first stage of the LeadScout pipeline. Given a city/state string
and a search radius, it:

1. Geocodes the location to a lat/lng (Geocoding API).
2. Calls the Places Nearby Search (New) endpoint, filtered to the requested
   business types (defaults to restaurants).
3. Paginates through `nextPageToken` until exhausted, respecting Google's
   ~2s delay before a fresh page token becomes valid.
4. Parses each result into a `Business` dataclass with `url_source` /
   `url_classification` set based on whether the API returned a website URL.

Phone numbers are deferred to the audit stage (a separate Place Details
call per business would burn the free quota faster than we want).
"""

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from leadscout.api import create_client, with_api_retry
from leadscout.config import DEFAULT_BUSINESS_TYPES, PLACES_API_SLEEP
from leadscout.exceptions import APIError
from leadscout.models import Business, UrlClassification, UrlSource

# TYPE_CHECKING block: PlacesQuotaTracker is referenced only in type
# annotations on optional parameters. Importing it at runtime would
# create a hard dependency just to satisfy a hint. The string form
# of the annotations resolves correctly under `from typing import
# get_type_hints` without needing this import to be eager.
if TYPE_CHECKING:
    from leadscout.places_quota import PlacesQuotaTracker

logger = logging.getLogger(__name__)


# --- API endpoints ---
# Geocoding API converts a human-readable address ("Santa Rosa Beach, FL")
# into latitude/longitude coordinates that the Places API needs.
GEOCODE_ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"
# Places Nearby Search (NEW). The legacy endpoint exists at a different URL
# and uses a different request/response shape; do not confuse them.
NEARBY_SEARCH_ENDPOINT = "https://places.googleapis.com/v1/places:searchNearby"

# --- Field mask ---
# The Places API (New) requires a header listing exactly which fields you
# want back. Without it you get billed for the full response but receive
# almost nothing.
#
# Note: Nearby Search (New) does NOT support pagination -- it returns up
# to 20 results in a single call with no nextPageToken. Including
# `nextPageToken` in the field mask causes a 400 INVALID_ARGUMENT
# ("Cannot find matching fields for path 'nextPageToken'"). The
# pagination loop in `_search_nearby` below still exists for safety
# (and to support a future swap to Text Search, which DOES paginate),
# but it just runs once for Nearby Search and exits when no token comes
# back -- which is always.
FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.nationalPhoneNumber,"
    "places.websiteUri,"
    "places.rating,"
    "places.userRatingCount,"
    "places.businessStatus,"
    "places.types"
)

# --- Pagination delay ---
# Google's docs: a freshly-issued nextPageToken is not immediately usable;
# calling with it too soon returns INVALID_ARGUMENT. 2s is the documented
# minimum; we use 2.5s to give a small safety margin.
NEXT_PAGE_TOKEN_DELAY = 2.5

def search_places(
    location: str,
    radius: int,
    api_key: str,
    *,
    included_types: list[str] | None = None,
    quota: "PlacesQuotaTracker | None" = None,
) -> list[Business]:
    """Discover businesses near a location.

    Geocodes the location string, then runs Places Nearby Search around the
    resulting coordinates. Returns a list of Business dataclasses, one per
    business found across all paginated result pages.

    Args:
        included_types: Google Places "Table A" type strings to filter by
            (e.g. ["restaurant"], ["dentist", "doctor"]). Defaults to
            DEFAULT_BUSINESS_TYPES from config.py.
        quota: Optional PlacesQuotaTracker. When supplied, every Places
            (New) request (geocoding included) calls quota.consume() first
            and raises APIError if the daily safe limit is reached. The
            single-shot `search` and `run` CLI commands pass None (no
            campaign-level cost ceiling). The campaign command always
            passes a tracker.

    Raises APIError on unrecoverable failures (bad API key, exhausted quota
    after retries, geocoding failure, no results for the location). Transient
    failures are retried automatically by `with_api_retry` in `api.py`.
    """
    # Fall back to config default when the caller doesn't specify types.
    if included_types is None:
        included_types = DEFAULT_BUSINESS_TYPES

    # `with` ensures the underlying connection pool is closed even if an
    # exception escapes. Both endpoints share the same client.
    with create_client() as client:
        # Geocoding is a separate Maps Platform SKU but billed against
        # the same monthly credit. Count it under the same tracker so
        # the budget reflects total Maps API spend, not just Places.
        if quota is not None and not quota.consume():
            raise APIError(
                "Places daily safe limit reached; campaign halted before "
                "geocoding. Resumes at UTC midnight."
            )
        lat, lng = _geocode(client, location, api_key)
        return _search_nearby(
            client, lat, lng, radius, api_key, included_types, quota=quota,
        )


# --- Geocoding ---
# Wrapping the raw HTTP call (and only the HTTP call) in @with_api_retry
# means tenacity sees a clean httpx exception on failure and can decide
# whether to retry. The status-handling layer above lives in _geocode.


@with_api_retry()
def _geocode_request(client: httpx.Client, location: str, api_key: str) -> dict:
    """Single HTTP GET to the Geocoding endpoint. Returns the parsed JSON body.

    Decorated with @with_api_retry so transport errors and retryable HTTP
    statuses (429, 5xx) automatically retry with exponential backoff.
    raise_for_status() converts a non-2xx response into HTTPStatusError,
    which the retry predicate inspects.
    """
    # GET request with the address and API key as query params. Google's
    # Geocoding API expects credentials in `key=` rather than a header.
    response = client.get(
        GEOCODE_ENDPOINT,
        params={"address": location, "key": api_key},
    )
    # Triggers HTTPStatusError on 4xx/5xx, which tenacity will inspect.
    response.raise_for_status()
    # .json() parses the response body as JSON; raises if the body isn't
    # valid JSON (which would itself be a permanent failure, not retried).
    return response.json()


def _geocode(client: httpx.Client, location: str, api_key: str) -> tuple[float, float]:
    """Translate a location string into (lat, lng) coordinates.

    Wraps _geocode_request so we can inspect the API's payload-level status
    field (separate from HTTP status). Geocoding has its own status taxonomy:
    "OK", "ZERO_RESULTS", "INVALID_REQUEST", "REQUEST_DENIED", etc.
    """
    data = _geocode_request(client, location, api_key)
    # Google returns its own status string in the body even on HTTP 200.
    # ZERO_RESULTS means the address didn't match any place; we treat it
    # as a user-facing error and exit cleanly.
    status = data.get("status")
    if status == "ZERO_RESULTS":
        raise APIError(f"Geocoding returned no results for {location!r}")
    if status != "OK":
        raise APIError(f"Geocoding failed for {location!r}: {status}")
    # `results` is a list; for our purposes the first entry is good enough.
    results = data.get("results") or []
    if not results:
        raise APIError(f"Geocoding succeeded but returned empty results for {location!r}")
    # Drill into the geometry payload for lat/lng floats.
    loc = results[0]["geometry"]["location"]
    lat, lng = loc["lat"], loc["lng"]
    logger.info("Geocoded %r to (%.6f, %.6f)", location, lat, lng)
    return lat, lng


# --- Nearby Search ---


@with_api_retry()
def _nearby_request(
    client: httpx.Client,
    lat: float,
    lng: float,
    radius: int,
    api_key: str,
    included_types: list[str],
    page_token: str | None = None,
) -> dict:
    """Single POST to the Places Nearby Search (New) endpoint. Returns parsed JSON.

    The (New) API expects:
    - API key in the X-Goog-Api-Key header (not a query param like the legacy API)
    - A field mask in X-Goog-FieldMask telling Google what to return
    - A JSON body describing the location restriction and type filter
    """
    headers = {
        # Required: tells Google what fields to populate in the response.
        # Without this we still get billed but receive almost no data.
        "X-Goog-FieldMask": FIELD_MASK,
        # Required: API authentication for the (New) Places API.
        "X-Goog-Api-Key": api_key,
    }
    # Body schema is documented at the URL in the module docstring.
    # `includedTypes` filters to one or more Places "Table A" types
    # (e.g. ["restaurant"], ["dentist", "doctor"]).
    # `locationRestriction.circle` constrains the search to a circular
    # area; radius is in meters.
    body: dict = {
        "includedTypes": included_types,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                # The API expects a float; cast to be explicit even when
                # the caller passed an int.
                "radius": float(radius),
            }
        },
    }
    # Pagination: subsequent pages reuse all the body fields plus the token
    # from the previous response.
    if page_token:
        body["pageToken"] = page_token
    response = client.post(NEARBY_SEARCH_ENDPOINT, headers=headers, json=body)
    response.raise_for_status()
    return response.json()


def _search_nearby(
    client: httpx.Client,
    lat: float,
    lng: float,
    radius: int,
    api_key: str,
    included_types: list[str],
    *,
    quota: "PlacesQuotaTracker | None" = None,
) -> list[Business]:
    """Run paginated Nearby Search and return parsed Business objects.

    Loops until the API stops returning a nextPageToken. Sleeps between
    pages: a small `PLACES_API_SLEEP` between every request, plus the
    documented `NEXT_PAGE_TOKEN_DELAY` before reusing a freshly-issued token.

    Catches the auth/quota HTTP errors that tenacity ultimately couldn't
    recover from and re-raises them as APIError so the CLI can surface a
    clean message.

    quota: optional PlacesQuotaTracker; when supplied, each request body
    issued by this function increments the tracker first. If the safe
    limit is reached mid-pagination we stop and return what we've collected
    so far rather than raising — partial results are still useful, the
    pagination loop only triggers in practice for Text Search anyway
    (Nearby Search returns all 20 results in one shot, no token).
    """
    businesses: list[Business] = []
    page_token: str | None = None
    page_index = 0
    # `while True` loop with explicit break inside; clearer than maintaining
    # a loop condition that mirrors the in-flight pagination state.
    while True:
        # Before reusing a page token we MUST wait. Google rejects a token
        # that's used too soon with INVALID_ARGUMENT. The very first request
        # has no token, so we skip the wait then.
        if page_token:
            time.sleep(NEXT_PAGE_TOKEN_DELAY)

        # Quota check before issuing the request. Halting here (rather
        # than raising) keeps the partial result set the caller already
        # paid for. In practice this only fires for the rare Text-Search
        # multi-page case; Nearby Search single-shots and won't loop.
        if quota is not None and not quota.consume():
            logger.warning(
                "Places quota at safe limit; stopping Nearby pagination "
                "with %d results collected.",
                len(businesses),
            )
            break

        page_index += 1
        try:
            data = _nearby_request(
                client, lat, lng, radius, api_key, included_types,
                page_token=page_token,
            )
        except httpx.HTTPStatusError as e:
            # tenacity has already exhausted retries on retryable codes
            # (429/5xx). Turn the underlying error into a clean APIError
            # with a hint for common cases the user might hit.
            status = e.response.status_code
            # Capture a body excerpt; Places returns a JSON error
            # describing exactly what's wrong (invalid field mask,
            # malformed body, etc.) and surfacing it here makes the
            # CLI user-debuggable without needing to reproduce.
            body_excerpt = (e.response.text or "")[:1500]
            if status in (401, 403):
                raise APIError(
                    f"Google Places API auth failure (HTTP {status}). "
                    "Check GOOGLE_PLACES_API_KEY."
                ) from e
            if status == 429:
                raise APIError(
                    "Google Places API quota exceeded (HTTP 429). "
                    "Wait and try again, or check your billing."
                ) from e
            raise APIError(
                f"Google Places API request failed with HTTP {status}: {body_excerpt}"
            ) from e

        # `places` is missing entirely on an empty response; default to []
        # so the for-loop just no-ops in that case.
        places = data.get("places") or []
        for place in places:
            businesses.append(_parse_place(place))
        logger.debug(
            "Page %d: fetched %d places (running total: %d)",
            page_index, len(places), len(businesses),
        )

        # nextPageToken is absent when there are no more pages. Break out.
        page_token = data.get("nextPageToken")
        if not page_token:
            break

        # Conservative inter-page throttle on top of the token delay above.
        time.sleep(PLACES_API_SLEEP)

    # Summary log so the operator can see at a glance what came back.
    with_website = sum(1 for b in businesses if b.website)
    without_website = len(businesses) - with_website
    logger.info(
        "Places search complete: %d businesses across %d page(s) "
        "(%d with website, %d without)",
        len(businesses), page_index, with_website, without_website,
    )
    return businesses


def _parse_place(place: dict) -> Business:
    """Convert one Place from the Nearby Search response into a Business.

    Field mapping:
    - `id` -> place_id (unique key Google assigns to each place)
    - `displayName.text` -> name (displayName is a {text, languageCode} object)
    - `formattedAddress` -> address
    - `nationalPhoneNumber` -> phone (only sometimes present in Nearby; we
      take it when offered, otherwise leave blank for the audit stage)
    - `websiteUri` -> website (drives url_source/url_classification below)
    - `rating` -> rating (1-5 scale, average of all user ratings)
    - `userRatingCount` -> review_count (total ratings; feature 06's
      dead-business filter drops `review_count == 0` listings)
    - `businessStatus` -> business_status ("OPERATIONAL" / "CLOSED_*"
      / missing); feature 06 may filter on this
    - `types[0]` -> business_type (first entry is the most specific type)
    """
    # Defensive .get() everywhere because Google sometimes omits fields.
    # Defaults match the Business dataclass defaults (empty string / None)
    # so a missing field round-trips as "no data" rather than crashing.
    place_id = place.get("id", "")
    # displayName is a dict like {"text": "Burger Joint", "languageCode": "en"}.
    # We only need the text. Two .get()s with a {} fallback so a missing
    # displayName doesn't raise.
    name = place.get("displayName", {}).get("text", "")
    address = place.get("formattedAddress", "")
    phone = place.get("nationalPhoneNumber", "")
    rating = place.get("rating")
    # Defaults to 0 (matches Business dataclass) so a missing field
    # serializes the same way an explicit zero would. int() coerces
    # in case the API returns a string-encoded number for any reason.
    review_count = int(place.get("userRatingCount") or 0)
    business_status = place.get("businessStatus", "")
    website = place.get("websiteUri", "")
    types = place.get("types") or []
    # First type is typically the most specific (e.g., "seafood_restaurant"
    # before the generic "restaurant"). Empty list -> empty string default.
    business_type = types[0] if types else ""

    # Whether Google gave us a website determines our initial classification.
    # `official_site` is a best-guess; feature 03 (URL discovery) will
    # reclassify entries that turn out to be Yelp/Facebook URLs.
    if website:
        url_source = UrlSource.GOOGLE_PLACES
        url_classification = UrlClassification.OFFICIAL_SITE
    else:
        url_source = UrlSource.NONE
        url_classification = UrlClassification.NONE

    # datetime.now(timezone.utc) gives a timezone-aware UTC timestamp. We
    # always store UTC (avoids ambiguity across timezones) and tag it as
    # tz-aware so .isoformat() emits an explicit "+00:00" suffix.
    return Business(
        place_id=place_id,
        name=name,
        address=address,
        phone=phone,
        website=website,
        url_source=url_source,
        url_classification=url_classification,
        rating=rating,
        review_count=review_count,
        business_type=business_type,
        business_status=business_status,
        last_scanned=datetime.now(timezone.utc),
    )
