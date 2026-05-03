"""URL discovery + classification (feature 03).

Two responsibilities:

1. **Discover** websites for businesses that came back from feature 02
   without one, by querying the Google Custom Search JSON API and
   fuzzy-matching the top results against the business name.

2. **Classify** every URL we end up with (Places-sourced or just
   discovered) into `official_site` / `social_media` / `directory_listing`
   based on domain matching. This catches misclassifications from
   feature 02, where a Google Places listing might link to a Facebook
   page that we initially flagged as `official_site`.

Free-tier safety: Custom Search has a hard 100 queries/day cap with no
billing fallback. The QuotaTracker class persists query count to a
JSON file (date-keyed) so multiple runs in the same UTC day share a
single budget. We stop at SAFE_LIMIT (95) to leave a margin.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from rapidfuzz import fuzz

from leadscout.api import create_client, with_api_retry
from leadscout.config import (
    CUSTOM_SEARCH_DAILY_LIMIT,
    CUSTOM_SEARCH_SAFE_LIMIT,
    CUSTOM_SEARCH_WARN_THRESHOLD,
    DIRECTORY_DOMAINS,
    FUZZY_MATCH_THRESHOLD,
    SOCIAL_MEDIA_DOMAINS,
)
from leadscout.exceptions import APIError
from leadscout.models import Business, UrlClassification, UrlSource
from leadscout.storage import atomic_write_text

logger = logging.getLogger(__name__)


# --- Custom Search endpoint ---
CUSTOM_SEARCH_ENDPOINT = "https://www.googleapis.com/customsearch/v1"

# Number of search results to inspect per business. The API allows up
# to 10; we use 5 because anything past the top few is unlikely to be
# the business's actual site.
TOP_N_RESULTS = 5

# --- Address parsing ---
# Anchored at end of string. Matches ", <city>, <ST> <ZIP>[+4][, USA]"
# - `,\s*` between fields (comma + optional whitespace)
# - `[^,]+?`  city: any chars except comma, lazy so trailing whitespace doesn't bleed
# - `[A-Z]{2}` two-letter state code (re.IGNORECASE handles lowercase)
# - `\d{5}(?:-\d{4})?` 5-digit ZIP, optional ZIP+4 suffix
# - `(?:,\s*USA?)?` optional ", USA" / ", US" suffix
# - `\s*$` trailing whitespace, then end of string
ADDRESS_RE = re.compile(
    r",\s*([^,]+?),\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?(?:,\s*USA?)?\s*$",
    re.IGNORECASE,
)

# --- Quota file ---
# Lives in the data dir. Leading dot keeps it visually separate from
# the per-location data files. Format: {"date": "YYYY-MM-DD", "count": N}.
QUOTA_FILENAME = ".custom_search_quota.json"


# ---------------------------------------------------------------------------
# QuotaTracker: persistent daily counter for Custom Search queries.
# ---------------------------------------------------------------------------


class QuotaTracker:
    """Counts Custom Search queries against the free-tier daily cap.

    Persists state to <data_dir>/.custom_search_quota.json so multiple
    runs in the same UTC day share one budget. Resets to 0 when the
    stored date no longer matches today (UTC). On corrupt state, defaults
    to "at limit" so we never accidentally blow past the free tier
    because of an unparseable counter file.

    Use:
        quota = QuotaTracker(data_dir)
        if quota.consume():
            # ok to issue one query
        else:
            # daily budget exhausted; skip this business
    """

    def __init__(self, data_dir: Path) -> None:
        # Resolve the file location once. Path / handles separator
        # differences across OSes for free.
        self.path = data_dir / QUOTA_FILENAME
        # Initialize to today, count 0; _load() will overwrite if a
        # valid prior state exists for today.
        self.date = self._today()
        self.count = 0
        self._load()

    @staticmethod
    def _today() -> str:
        """ISO date string for today in UTC. We use UTC because that's
        when Google's quota resets, regardless of the user's local TZ."""
        return datetime.now(timezone.utc).date().isoformat()

    def _load(self) -> None:
        """Load count from disk. Defensive on corruption."""
        today = self._today()
        # No file = first run today. Stay at count=0.
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            file_date = data["date"]
            file_count = int(data["count"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            # Defensive default: assume we're at the limit. Better to
            # skip queries we could've made than to overrun the cap
            # because the counter file got corrupted somehow.
            logger.warning(
                "Custom Search quota file at %s is corrupt (%s). "
                "Refusing all queries this run; delete the file to reset.",
                self.path, e,
            )
            self.count = CUSTOM_SEARCH_SAFE_LIMIT
            return
        # Stored counter from a previous UTC day -> new day, reset.
        if file_date != today:
            logger.info(
                "Custom Search quota date rolled over (%s -> %s); resetting count.",
                file_date, today,
            )
            self.count = 0
            self.date = today
            self._persist()
            return
        # Same UTC day as before; resume the previous count.
        self.count = file_count
        self.date = file_date

    def _persist(self) -> None:
        """Write current state to disk via atomic_write_text."""
        text = json.dumps({"date": self.date, "count": self.count})
        atomic_write_text(self.path, text)

    def consume(self) -> bool:
        """Try to claim one query against the daily budget.

        Returns True if claimed (caller may issue the query), False if
        the budget is exhausted (caller must skip).
        """
        if self.count >= CUSTOM_SEARCH_SAFE_LIMIT:
            return False
        self.count += 1
        self._persist()
        # Warn exactly once when crossing the threshold. Comparing
        # equality (==) instead of >= so we don't log every subsequent
        # query for the rest of the run.
        if self.count == CUSTOM_SEARCH_WARN_THRESHOLD:
            logger.warning(
                "Custom Search quota at %d/%d (free-tier limit %d). "
                "Discovery will halt at %d.",
                self.count, CUSTOM_SEARCH_SAFE_LIMIT,
                CUSTOM_SEARCH_DAILY_LIMIT, CUSTOM_SEARCH_SAFE_LIMIT,
            )
        return True

    @property
    def remaining(self) -> int:
        """How many queries we can still issue today before stopping."""
        return max(0, CUSTOM_SEARCH_SAFE_LIMIT - self.count)


# ---------------------------------------------------------------------------
# URL classification: bucket a URL into one of the UrlClassification enum values.
# ---------------------------------------------------------------------------


def _classify_url(url: str) -> UrlClassification:
    """Classify a URL as social_media / directory_listing / official_site.

    Empty URL returns NONE. Otherwise we extract the netloc (host part)
    and substring-match against the configured domain lists. Substring
    match (rather than exact match) handles subdomains: m.facebook.com
    still classifies as social_media because "facebook.com" is contained
    in it.
    """
    if not url:
        return UrlClassification.NONE
    # urlparse needs a scheme to populate `netloc`. If the URL came from
    # the search results' `displayLink`, it might be just "yelp.com/biz/x"
    # with no scheme. Synthesize one so urlparse finds the host.
    if "://" not in url:
        url = f"http://{url}"
    netloc = urlparse(url).netloc.lower()
    # Strip a leading "www." so we compare against the canonical host.
    # Using removeprefix (3.9+) is more direct than slicing.
    netloc = netloc.removeprefix("www.")
    # any() short-circuits on first match. Order matters: social and
    # directory both take precedence over the official_site fallback,
    # but they're disjoint sets so their internal order doesn't matter.
    if any(d in netloc for d in SOCIAL_MEDIA_DOMAINS):
        return UrlClassification.SOCIAL_MEDIA
    if any(d in netloc for d in DIRECTORY_DOMAINS):
        return UrlClassification.DIRECTORY_LISTING
    return UrlClassification.OFFICIAL_SITE


# ---------------------------------------------------------------------------
# Address parsing: extract city/state from Business.address.
# ---------------------------------------------------------------------------


def _extract_city_state(address: str) -> tuple[str, str] | None:
    """Pull (city, state) out of a US-formatted address.

    Returns None on empty / non-US / unparseable input. The caller
    should fall back to a query without location context in that case.
    """
    if not address:
        return None
    match = ADDRESS_RE.search(address)
    if not match:
        return None
    # Group 1 = city; group 2 = state. .strip() defends against extra
    # whitespace; .upper() normalizes state casing in case the regex
    # matched lowercase.
    return match.group(1).strip(), match.group(2).upper()


# ---------------------------------------------------------------------------
# Result matching: pick the best Custom Search hit for a business.
# ---------------------------------------------------------------------------


def _match_results(
    business_name: str, items: list[dict]
) -> tuple[str, UrlClassification] | None:
    """Find the best URL match for a business among Custom Search results.

    For each of the top TOP_N_RESULTS items, we score the business name
    against both the result title and its displayLink, taking the max.
    Anything below FUZZY_MATCH_THRESHOLD is discarded.

    Among matches that survive the threshold, we prefer the first one
    that classifies as `official_site` (the spec's tiebreaker for
    multi-presence businesses with both a real site and a Facebook page).
    If no surviving match is `official_site`, return the first match.

    Returns (url, classification) on hit, None on no match.
    """
    matches: list[tuple[str, UrlClassification, int]] = []
    # Slice to TOP_N_RESULTS so unexpected longer responses don't change
    # behavior; the search request also caps at 5 via num=5.
    for item in items[:TOP_N_RESULTS]:
        title = item.get("title", "") or ""
        display_link = item.get("displayLink", "") or ""
        # Two complementary fuzzy scorers, max wins:
        # - token_sort_ratio: handles word-order variation
        #   ("The Red Bar" vs "Red Bar, The" scores 100). Weak when
        #   the target has lots of extra words ("Coastal Catch" vs
        #   "Coastal Catch | Fresh Seafood in Santa Rosa Beach"
        #   scores ~28 because the extra tokens dilute the alignment).
        # - partial_ratio: handles substring/embedded matches (the
        #   business name appearing inside a longer title or domain).
        #   Strong on long-target cases, weaker on word-order shuffles.
        # Spec asks for token_sort_ratio at threshold 70, but the
        # spec's own example test cases (substring matches in long
        # titles) only pass when we also consider partial_ratio.
        score = max(
            fuzz.token_sort_ratio(business_name, title),
            fuzz.token_sort_ratio(business_name, display_link),
            fuzz.partial_ratio(business_name, title),
            fuzz.partial_ratio(business_name, display_link),
        )
        if score < FUZZY_MATCH_THRESHOLD:
            continue
        # Prefer the full link (with scheme/path) over displayLink for
        # storage; fall back to displayLink only when link is missing.
        url = item.get("link") or display_link
        if not url:
            continue
        classification = _classify_url(url)
        matches.append((url, classification, score))

    if not matches:
        return None
    # Tiebreaker: first official_site wins. Without this, a Facebook page
    # listed above a real website would be picked just because it scored
    # marginally higher on the title comparison.
    for url, classification, _score in matches:
        if classification == UrlClassification.OFFICIAL_SITE:
            return url, classification
    # All matches are social/directory; return the first match's URL.
    url, classification, _ = matches[0]
    return url, classification


# ---------------------------------------------------------------------------
# Custom Search HTTP layer.
# ---------------------------------------------------------------------------


@with_api_retry()
def _custom_search_request(
    client: httpx.Client, api_key: str, cx: str, query: str
) -> dict:
    """Single GET to the Custom Search JSON API. Returns parsed body.

    Decorated with @with_api_retry so 5xx and transport errors retry
    transparently. 4xx (auth, bad CX) re-raises immediately.
    """
    response = client.get(
        CUSTOM_SEARCH_ENDPOINT,
        # `num` caps the result count at 5; saves a tiny amount of
        # bandwidth and matches what _match_results actually inspects.
        params={"key": api_key, "cx": cx, "q": query, "num": TOP_N_RESULTS},
    )
    response.raise_for_status()
    return response.json()


def _build_query(business: Business) -> str:
    """Build the Custom Search query string for one business.

    Format: '"<name>" "<city>, <state>"' if we can extract a US city/state
    from the formatted address. Otherwise just '"<name>"' (still useful;
    just less precise).
    """
    # Quoting the name forces Google to match it as a phrase rather than
    # individual tokens. That's important for short or generic names
    # ("The Diner") that would otherwise drown in unrelated results.
    name_part = f'"{business.name}"'
    location = _extract_city_state(business.address)
    if location is None:
        return name_part
    city, state = location
    return f'{name_part} "{city}, {state}"'


# ---------------------------------------------------------------------------
# Top-level orchestration: discover_urls.
# ---------------------------------------------------------------------------


def discover_urls(
    businesses: list[Business],
    api_key: str,
    cx: str,
    *,
    data_dir: Path,
    force: bool = False,
) -> list[Business]:
    """Discover and classify website URLs for a batch of businesses.

    For businesses with `url_source == NONE` (or all of them when
    `force=True`), runs a Custom Search query and updates the matching
    business's `website` / `url_source` / `url_classification`.

    For ALL businesses with a non-empty `website`, recomputes
    `url_classification` based on the URL's domain. This corrects the
    placeholder classification feature 02 stamps on Places-sourced URLs.

    Mutates the input list in place AND returns it (callers can pick
    whichever style they prefer; we return for chainability).

    Raises APIError on auth/quota failures from Custom Search after
    tenacity retries are exhausted.
    """
    quota = QuotaTracker(data_dir)
    logger.info(
        "Discovery starting: %d businesses, quota remaining today: %d",
        len(businesses), quota.remaining,
    )

    # Single client for the whole batch; reuses connection pool.
    with create_client() as client:
        for biz in businesses:
            # --- Step 1: search if needed ---
            # Skip already-discovered/owned URLs unless --force.
            needs_search = force or biz.url_source == UrlSource.NONE
            if needs_search:
                _maybe_search_one(biz, client, api_key, cx, quota)

            # --- Step 2: classify any URL we have ---
            # Runs unconditionally for businesses with a website, so we
            # catch URLs that came in mis-classified from feature 02.
            if biz.website:
                new_class = _classify_url(biz.website)
                if new_class != biz.url_classification:
                    logger.info(
                        "Reclassified %s: %s -> %s",
                        biz.name,
                        biz.url_classification.value,
                        new_class.value,
                    )
                biz.url_classification = new_class

    return businesses


def _maybe_search_one(
    biz: Business,
    client: httpx.Client,
    api_key: str,
    cx: str,
    quota: QuotaTracker,
) -> None:
    """Run one Custom Search query for a business, mutating it in place
    if a match is found. Side-effects only; no return value.

    Extracted from the main loop so the orchestration above stays
    readable and the per-business steps fit on one screen each.
    """
    # Quota check FIRST so we never even build the query if we're done
    # for the day. consume() returns False when we're at SAFE_LIMIT.
    if not quota.consume():
        logger.info(
            "Discovery skipped (daily Custom Search limit reached): %s",
            biz.name,
        )
        return

    query = _build_query(biz)
    logger.debug("Searching %s with query=%s", biz.name, query)

    try:
        data = _custom_search_request(client, api_key, cx, query)
    except httpx.HTTPStatusError as e:
        # tenacity already exhausted retries on transient codes. Wrap
        # auth/quota failures into APIError so the CLI can surface a
        # clean message.
        status = e.response.status_code
        if status in (401, 403):
            raise APIError(
                f"Custom Search auth failure (HTTP {status}). "
                "Check GOOGLE_CUSTOM_SEARCH_API_KEY and GOOGLE_CUSTOM_SEARCH_CX."
            ) from e
        if status == 429:
            raise APIError(
                "Custom Search quota exceeded (HTTP 429). "
                "Daily free-tier cap is 100 queries; resets at UTC midnight."
            ) from e
        raise APIError(f"Custom Search request failed: HTTP {status}") from e

    items = data.get("items") or []
    match = _match_results(biz.name, items)
    if match is None:
        logger.info("No discovery match for %s (%d results scanned)", biz.name, len(items))
        return

    url, classification = match
    biz.website = url
    biz.url_source = UrlSource.SEARCH_DISCOVERED
    biz.url_classification = classification
    logger.info(
        "Discovered for %s: %s [%s]", biz.name, url, classification.value
    )
