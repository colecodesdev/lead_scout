"""Website audit (feature 04): PageSpeed Insights + Playwright DOM checks.

Two layers per business with `url_classification == official_site`:

1. **PageSpeed Insights** (remote): one call per strategy (mobile + desktop)
   to ``https://www.googleapis.com/pagespeedonline/v5/runPagespeed``.
   We extract the four Lighthouse category scores (performance,
   accessibility, seo, best-practices) into per-strategy dicts on the
   `Audit`. Uses a key from `GOOGLE_PAGESPEED_API_KEY` (preferred), or
   `GOOGLE_PLACES_API_KEY` (fallback), or unauthenticated (lowest quota).

2. **Playwright DOM** (local headless Chromium at iPhone viewport):
   navigates with a domcontentloaded -> networkidle fallback so persistent
   WebSockets/analytics don't hang us. Records bool DOM checks
   (menu/hours/contact/viewport/SSL/online-ordering/reservation), wall-clock
   load time, and any image/script/stylesheet requests that returned 4xx/5xx
   via a `page.on('response')` listener registered before navigation.

`deficiencies` is a list of stable, plain-English strings derived from the
audit results. Feature 05 (scoring) consumes those rather than re-deriving
the same logic from raw fields.

As a side benefit, the DOM scrape backfills `Business.phone` with the
first phone match if it was empty (e.g., the Places search step didn't
include a phone for that business).
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from leadscout.api import create_client, with_api_retry
from leadscout.config import (
    AUDIT_FRESHNESS_DAYS,
    AUDIT_NETWORKIDLE_TIMEOUT_MS,
    AUDIT_PAGE_TIMEOUT_MS,
    AUDIT_PERFORMANCE_THRESHOLD,
    AUDIT_USER_AGENT,
    AUDIT_VIEWPORT_HEIGHT,
    AUDIT_VIEWPORT_WIDTH,
    PSI_API_SLEEP,
)
from leadscout.exceptions import APIError, AuditError
from leadscout.models import Audit, Business, UrlClassification

logger = logging.getLogger(__name__)


# --- PageSpeed Insights ---
PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Map our snake_case category names to the API's keys. The "best-practices"
# category uses a hyphen in the JSON, which would be invalid as a Python
# attribute name; we normalize at parse time.
_PSI_CATEGORY_API_KEYS = {
    "performance": "performance",
    "accessibility": "accessibility",
    "seo": "seo",
    "best_practices": "best-practices",
}


# --- DOM patterns ---
# Used by both _run_dom_checks and the standalone DOM-string helpers
# (_check_viewport, _has_hours_text) so tests can exercise the regex
# without launching Chromium.
PHONE_PATTERN = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
HOURS_RE = re.compile(
    r"\bMon\b|\bTue\b|\bWed\b|\bThu\b|\bFri\b|\bSat\b|\bSun\b"
    r"|\bAM\b|\bPM\b|\bHours\b|\bOpen\b",
    re.IGNORECASE,
)
ONLINE_ORDERING_PATTERNS = (
    "order online",
    "order now",
    "online ordering",
    "toasttab.com",
    "squareup.com",
    "chownow",
)
RESERVATION_PATTERNS = (
    "reservation",
    "book a table",
    "opentable",
    "resy",
)
VIEWPORT_RE = re.compile(
    r'<meta\s+name=["\']viewport["\'][^>]*width=device-width',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Top-level orchestration: audit_websites.
# ---------------------------------------------------------------------------


def audit_websites(
    businesses: list[Business],
    api_key: str | None = None,
    *,
    force: bool = False,
) -> list[Business]:
    """Audit every business with an official-site URL. Mutates in place.

    api_key: optional PSI key. Caller is expected to resolve the env-var
    chain (PSI key -> Places key -> None for unauthenticated).

    force: if True, re-audit even businesses with an audit fresher than
    AUDIT_FRESHNESS_DAYS. Default is to skip them.

    Mirrors the discover_urls pattern: takes Business list, mutates the
    `audit` field on each, and returns the same list for chainability.
    """
    # Lazy import: playwright is heavy and the rest of the codebase
    # doesn't need it. Keeping it inside the function also means importing
    # leadscout.audit doesn't fail when chromium isn't installed yet
    # (the failure surfaces only when audit_websites actually runs).
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise AuditError(
            "playwright is not installed. Run `uv sync` to install dependencies."
        ) from e

    now = datetime.now(timezone.utc)
    fresh_cutoff = now - timedelta(days=AUDIT_FRESHNESS_DAYS)

    # Pre-filter eligible businesses so we don't even open a browser
    # if there's nothing to do (e.g., the dataset has no official_site
    # URLs yet).
    eligible = [
        b for b in businesses if _is_eligible(b, fresh_cutoff, force)
    ]

    logger.info(
        "Audit eligible: %d of %d businesses (force=%s)",
        len(eligible), len(businesses), force,
    )
    if not eligible:
        return businesses

    # One httpx client and one Chromium browser for the whole batch;
    # creating either per-business would be wasteful. The context (cookies
    # + viewport) IS per-business, recreated inside _run_dom_checks.
    with create_client() as http_client:
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    for i, biz in enumerate(eligible):
                        # Inter-business gap covers PSI's rate limit
                        # (4s between requests stays under 25 req/100s).
                        # Skip on the first iteration so we don't sleep
                        # before doing any work.
                        if i > 0:
                            time.sleep(PSI_API_SLEEP)
                        _audit_one(biz, http_client, browser, api_key)
                finally:
                    browser.close()
        except AuditError:
            raise
        except Exception as e:
            # playwright raises various exception types depending on what
            # went wrong (browser binary not installed -> Error;
            # connection failure -> different). Wrap them all so callers
            # get a single AuditError type.
            raise AuditError(f"Browser session failed: {e}") from e

    return businesses


def _is_eligible(
    biz: Business, fresh_cutoff: datetime, force: bool
) -> bool:
    """Decide whether to audit a given business.

    - Must have url_classification == OFFICIAL_SITE (yelp/facebook URLs
      have nothing actionable to audit).
    - Must not already have a fresh audit unless force=True.
    """
    if biz.url_classification != UrlClassification.OFFICIAL_SITE:
        return False
    if force:
        return True
    if biz.audit and biz.audit.audited_at:
        if biz.audit.audited_at >= fresh_cutoff:
            logger.debug(
                "Skipping %s: audit at %s is < %d days old",
                biz.name, biz.audit.audited_at, AUDIT_FRESHNESS_DAYS,
            )
            return False
    return True


def _audit_one(
    biz: Business,
    http_client: httpx.Client,
    browser,
    api_key: str | None,
) -> None:
    """Run PSI + Playwright on one business; attach the resulting Audit.

    PSI failures are logged but don't abort: a partial audit (PSI scores
    None, DOM checks populated, or vice versa) still has lead value.
    Playwright failures (Cloudflare block, timeout) similarly fall
    through with all bools False.
    """
    audit = Audit(audited_at=datetime.now(timezone.utc))

    # --- PSI mobile ---
    try:
        audit.lighthouse_mobile = _fetch_psi_scores(
            http_client, biz.website, "mobile", api_key
        )
    except APIError as e:
        logger.warning("PSI mobile failed for %s: %s", biz.name, e)

    # 4s gap between mobile and desktop calls; matches PSI_API_SLEEP.
    time.sleep(PSI_API_SLEEP)

    # --- PSI desktop ---
    try:
        audit.lighthouse_desktop = _fetch_psi_scores(
            http_client, biz.website, "desktop", api_key
        )
    except APIError as e:
        logger.warning("PSI desktop failed for %s: %s", biz.name, e)

    # --- Playwright DOM ---
    try:
        dom = _run_dom_checks(browser, biz.website)
    except AuditError as e:
        logger.warning("DOM audit failed for %s: %s", biz.name, e)
    else:
        audit.has_menu = dom["has_menu"]
        audit.has_hours = dom["has_hours"]
        audit.has_contact_info = dom["has_contact_info"]
        audit.has_mobile_viewport = dom["has_mobile_viewport"]
        audit.has_ssl = dom["has_ssl"]
        audit.has_online_ordering = dom["has_online_ordering"]
        audit.has_reservation = dom["has_reservation"]
        audit.load_time_seconds = dom["load_time_seconds"]
        audit.broken_assets = dom["broken_assets"]
        # Phone backfill: only fill if Business.phone was empty AND we
        # actually scraped a phone. Spec calls this a side benefit since
        # we're already on the page.
        if not biz.phone and dom.get("phone"):
            biz.phone = dom["phone"]
            logger.info("Backfilled phone for %s: %s", biz.name, biz.phone)

    audit.deficiencies = _compute_deficiencies(audit)
    biz.audit = audit
    logger.info(
        "Audited %s: %d deficiencies, mobile-perf=%s",
        biz.name,
        len(audit.deficiencies),
        audit.lighthouse_mobile.get("performance")
        if audit.lighthouse_mobile
        else None,
    )


# ---------------------------------------------------------------------------
# PageSpeed Insights layer.
# ---------------------------------------------------------------------------


@with_api_retry(base_wait=PSI_API_SLEEP)
def _psi_request(
    client: httpx.Client,
    url: str,
    strategy: str,
    api_key: str | None,
) -> dict:
    """One GET to PageSpeed Insights. Returns parsed JSON body.

    Decorated with @with_api_retry but with a longer base_wait override:
    PSI rate-limits more aggressively than Places, so the first retry
    waits ~PSI_API_SLEEP (4s) instead of the default 2s.
    """
    params: dict[str, str] = {"url": url, "strategy": strategy}
    # PSI works without auth at lower quota; skip the key param entirely
    # rather than passing an empty string (which the API may reject).
    if api_key:
        params["key"] = api_key
    response = client.get(PSI_ENDPOINT, params=params)
    response.raise_for_status()
    return response.json()


def _fetch_psi_scores(
    client: httpx.Client,
    url: str,
    strategy: str,
    api_key: str | None,
) -> dict:
    """Run a PSI request and parse it. Translates HTTP errors to APIError."""
    try:
        data = _psi_request(client, url, strategy, api_key)
    except httpx.HTTPStatusError as e:
        raise APIError(
            f"PSI {strategy} for {url}: HTTP {e.response.status_code}"
        ) from e
    return _parse_psi_scores(data)


def _parse_psi_scores(data: dict) -> dict:
    """Pure parser: extract Lighthouse category scores (0-100).

    PSI returns scores in [0, 1] under
    `lighthouseResult.categories.<key>.score`. We multiply by 100 and
    round so the stored number matches what users see in Lighthouse.
    Missing or null scores survive as None (e.g., a category that PSI
    couldn't evaluate for the page).
    """
    result: dict = {}
    categories = data.get("lighthouseResult", {}).get("categories", {})
    for our_key, api_key in _PSI_CATEGORY_API_KEYS.items():
        cat = categories.get(api_key) or {}
        score = cat.get("score")
        result[our_key] = None if score is None else round(score * 100)
    return result


# ---------------------------------------------------------------------------
# Playwright DOM layer.
# ---------------------------------------------------------------------------


def _run_dom_checks(browser, url: str) -> dict:
    """Run DOM checks against `url` using a fresh browser context.

    Returns a dict keyed by check name. Raises AuditError if the page
    can't be reached at all (timeout, Cloudflare 403, etc.) so the
    caller can record a partial audit.
    """
    # Fresh context per audit so cookies/storage don't leak between
    # sites. Mobile viewport + realistic UA configured at context level
    # (applies before any page loads).
    context = browser.new_context(
        viewport={
            "width": AUDIT_VIEWPORT_WIDTH,
            "height": AUDIT_VIEWPORT_HEIGHT,
        },
        user_agent=AUDIT_USER_AGENT,
    )
    try:
        page = context.new_page()
        # Listener MUST be registered BEFORE page.goto() or we'll miss
        # responses fired during navigation. The closure captures the
        # list so each response appends to it.
        broken_assets: list[dict] = []
        page.on(
            "response",
            lambda r: _maybe_record_broken_asset(r, broken_assets),
        )

        start = time.monotonic()
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=AUDIT_PAGE_TIMEOUT_MS,
            )
        except Exception as e:
            # PlaywrightTimeoutError, navigation aborts, network errors, etc.
            raise AuditError(f"navigation failed: {e}") from e

        # Cloudflare/anti-bot detection: 403 on the main document means
        # we got served a challenge page or block. Skip the rest of the
        # checks and let the caller record a partial audit.
        if response is not None and response.status == 403:
            raise AuditError("site blocked automated access (HTTP 403)")

        # Best-effort wait for network idle. Wrapped because persistent
        # WebSockets/analytics never settle on some sites.
        try:
            page.wait_for_load_state(
                "networkidle", timeout=AUDIT_NETWORKIDLE_TIMEOUT_MS
            )
        except Exception:
            logger.debug("networkidle wait timed out for %s; proceeding", url)

        load_time = time.monotonic() - start

        # Final URL after redirects: some sites HTTP -> HTTPS redirect,
        # so checking the original URL would miss SSL-supporting sites.
        final_url = page.url
        has_ssl = urlparse(final_url).scheme == "https"

        # Snapshot text and HTML once so all the substring checks
        # operate on consistent content.
        body_text = page.inner_text("body")
        body_lower = body_text.lower()
        html = page.content()

        phone_match = PHONE_PATTERN.search(body_text)
        phone = phone_match.group(0) if phone_match else None

        return {
            "has_menu": _check_menu(page, body_lower),
            "has_hours": bool(HOURS_RE.search(body_text)),
            "has_contact_info": phone is not None or "contact" in body_lower,
            "has_mobile_viewport": bool(VIEWPORT_RE.search(html)),
            "has_ssl": has_ssl,
            "has_online_ordering": any(
                p in body_lower for p in ONLINE_ORDERING_PATTERNS
            ),
            "has_reservation": any(
                p in body_lower for p in RESERVATION_PATTERNS
            ),
            "load_time_seconds": round(load_time, 2),
            "broken_assets": broken_assets,
            "phone": phone,
        }
    finally:
        context.close()


def _check_menu(page, body_lower: str) -> bool:
    """Detect a menu link or visible 'menu' text on the page.

    SPA-aware: a "menu" might be an anchor link (#menu) within the same
    page rather than a separate page. We check both visible text and
    href attributes so single-page restaurant sites still match.
    """
    if "menu" in body_lower:
        return True
    # Fall back to scanning hrefs in case "menu" only appears as a URL
    # fragment or path (e.g., href="/menu" with the link text being an
    # icon or image).
    links = page.locator("a")
    count = links.count()
    for i in range(count):
        href = links.nth(i).get_attribute("href") or ""
        if "menu" in href.lower():
            return True
    return False


def _maybe_record_broken_asset(response, broken_assets: list[dict]) -> None:
    """Append a broken-asset entry if the response is a 4xx/5xx asset.

    Filters to images/stylesheets/scripts only; XHR or fetch failures
    are noisy and rarely indicate a "broken site" (e.g., an analytics
    endpoint returning 410 doesn't make a restaurant site less usable).
    """
    status = response.status
    if status < 400:
        return
    request = response.request
    resource_type = request.resource_type
    if resource_type not in ("image", "stylesheet", "script"):
        return
    broken_assets.append(
        {
            "url": request.url,
            "status": status,
            "type": resource_type,
        }
    )


# ---------------------------------------------------------------------------
# Deficiency builder.
# ---------------------------------------------------------------------------


def _compute_deficiencies(audit: Audit) -> list[str]:
    """Translate audit fields into stable plain-English deficiency strings.

    Pure function; no I/O. Strings are stable so feature 05 (scoring)
    can match on them deterministically. Order is roughly "baseline
    hygiene -> restaurant essentials -> performance -> broken assets"
    so a human reading the list sees the most-fundamental issues first.
    """
    deficiencies: list[str] = []

    # Baseline hygiene: SSL and mobile viewport.
    if not audit.has_ssl:
        deficiencies.append("No SSL certificate")
    if not audit.has_mobile_viewport:
        deficiencies.append("No mobile viewport configured")

    # Restaurant essentials.
    if not audit.has_menu:
        deficiencies.append("No menu page found")
    if not audit.has_hours:
        deficiencies.append("No business hours found")
    if not audit.has_contact_info:
        deficiencies.append("No phone or contact info found")
    if not audit.has_online_ordering:
        deficiencies.append("No online ordering")
    if not audit.has_reservation:
        deficiencies.append("No reservation system")

    # Mobile performance: the score that matters most for restaurant
    # customers (overwhelmingly on phones).
    if audit.lighthouse_mobile:
        perf = audit.lighthouse_mobile.get("performance")
        if perf is not None and perf < AUDIT_PERFORMANCE_THRESHOLD:
            deficiencies.append(f"Mobile performance score: {perf}/100")

    # Broken assets: count by type so the lead pitch is specific
    # ("3 broken images" beats "3 broken assets").
    if audit.broken_assets:
        by_type: dict[str, int] = {}
        for a in audit.broken_assets:
            t = a.get("type", "asset")
            by_type[t] = by_type.get(t, 0) + 1
        for t, n in sorted(by_type.items()):
            # Pluralize correctly: "stylesheet" -> "stylesheets",
            # "image" -> "images". Skip if the type already ends in 's'.
            label = t if t.endswith("s") else f"{t}s"
            deficiencies.append(f"{n} broken {label}")

    return deficiencies
