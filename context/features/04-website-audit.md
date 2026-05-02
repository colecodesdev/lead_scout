# Website Audit Spec

## Overview

For every business with a URL classified as `"official_site"`, run a two-layer audit: remote Lighthouse scores via the PageSpeed Insights API and local DOM checks via Playwright headless browser. Produces an `Audit` record attached to the business with specific deficiencies flagged. Lives in `src/leadscout/audit.py` and wired to the `audit` CLI subcommand.

## Requirements

- `audit_websites(businesses: list[Business]) -> list[Audit]` as the main function. Only audits businesses where `url_classification == "official_site"`.
- **PageSpeed Insights layer:**
  - Hit `https://www.googleapis.com/pagespeedonline/v5/runPagespeed` for each URL
  - Run both `strategy=mobile` and `strategy=desktop` (two calls per business)
  - Extract category scores: performance, accessibility, SEO, best_practices (each 0-100)
  - Store as `lighthouse_mobile` and `lighthouse_desktop` dicts on the `Audit` object
  - Respect rate limit: 4 second gap between requests (stays under 25 req/100s)
  - PageSpeed Insights API does NOT require an API key for basic usage, but using one increases quota. Use the Places API key if set, fall back to unauthenticated.
- **Playwright DOM layer:**
  - Launch headless Chromium, navigate to the URL, wait for network idle
  - Check for presence of each item and set the corresponding bool on `Audit`:
    - `has_menu`: page contains a link or element with text matching "menu" (case-insensitive)
    - `has_hours`: page contains text matching common hours patterns ("Mon-Fri", "AM", "PM", "Hours", "Open")
    - `has_contact_info`: page contains a phone number pattern (`\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}`) or "Contact" link
    - `has_mobile_viewport`: `<meta name="viewport"` tag exists with `width=device-width`
    - `has_ssl`: final URL after redirects starts with `https://`
    - `has_online_ordering`: page contains links/text matching "order online", "order now", "online ordering", or common ordering platform embeds (Toast, Square, ChowNow)
    - `has_reservation`: page contains links/text matching "reservation", "book a table", "OpenTable", "Resy"
    - `load_time_seconds`: wall clock time from navigation start to network idle
    - `broken_assets`: collect any image/script/stylesheet requests that returned 4xx/5xx (via Playwright's `page.on("response")`)
  - Set browser viewport to 375x812 (iPhone size) for the Playwright check since mobile experience is what matters for restaurant sites
  - Timeout per site: 30 seconds. If a site doesn't load, log it and record `load_time_seconds = None`, all bools as `False`.
- Build `deficiencies` list from audit results: a plain-English string for each failure (e.g., "No menu page found", "Mobile performance score: 23/100", "No SSL certificate", "3 broken images")
- Wire to CLI: `leadscout audit --data-file data/santa_rosa_beach_fl.json`
- Skip businesses that already have an `Audit` with `audited_at` within the last 7 days unless `--force` flag is passed
- On completion, attach audits to business records and save via `storage.py`
- Log per-business: URL visited, Lighthouse scores, each DOM check result, total deficiencies

## Files to Create

1. `src/leadscout/audit.py`: PageSpeed + Playwright audit logic
2. `tests/test_audit.py`: Unit tests
3. `tests/fixtures/pagespeed_response.json`: Sample PageSpeed Insights API response
4. `tests/fixtures/pagespeed_poor_scores.json`: Sample response with bad scores

## Files to Modify

1. `src/leadscout/cli.py`: Wire up `audit` subcommand with `--data-file` and `--force` options
2. `src/leadscout/models.py`: Add Audit dataclass if not already present with full field set
3. `src/leadscout/storage.py`: Handle serialization of Audit records nested within or alongside Business records in JSON

## Key Gotchas

- PageSpeed Insights API without an API key: works but limited to ~5 req/100s. With an API key: ~25 req/100s. Since we run 2 calls per business (mobile + desktop), a scan of 30 businesses is 60 calls. At 4s gap = ~4 minutes. Acceptable for v1.
- Playwright must be installed separately: `playwright install chromium`. This is NOT a pip dependency. The `playwright` Python package is the SDK; the browser binary is a separate download. The scaffold spec should note this, and the CLI should check for it at startup and print a clear error if missing.
- `page.goto()` with `wait_until="networkidle"` can hang on sites with persistent WebSocket connections or analytics pings. Use `wait_until="domcontentloaded"` as fallback with a manual `page.wait_for_load_state("networkidle", timeout=10000)` wrapped in try/except.
- The `broken_assets` check via `page.on("response")` must be registered BEFORE `page.goto()`. Register the listener, then navigate.
- Some restaurant sites are single-page apps (Square Online, Wix, Squarespace). The "menu" link might be an anchor (`#menu`) not a separate page. The DOM check should look for both `<a href>` elements and visible text content matching the keywords.
- Sites behind Cloudflare or similar protection may block headless browsers. Playwright's default user-agent is detectable. Set a realistic Chrome user-agent string. If blocked (403 or challenge page), log it as "site blocked automated access" and skip the Playwright layer. The Lighthouse scores from PageSpeed Insights will still be available since that runs from Google's infrastructure.

## Environment Variables

```
# No new env vars. PageSpeed Insights uses GOOGLE_PLACES_API_KEY if set.
```

## Notes

- Businesses classified as `"social_media"` or `"directory_listing"` are NOT audited. There's nothing actionable in auditing a Yelp page. These businesses score as leads based on their lack of an official site, not on site quality.
- The 7-day skip window is intentional. Restaurant sites rarely change week to week. Re-auditing a site 24 hours later is wasted compute and API usage.
- Phone number extraction from Playwright can backfill `Business.phone` if it was `None` from the Places search step. This is a side benefit, not a requirement, but worth implementing since we're already on the page.

## Testing

1. Mock a PageSpeed response with known scores, run through audit parsing, assert `lighthouse_mobile` and `lighthouse_desktop` dicts match expected values
2. Create a minimal HTML page (local file) with a "Menu" link, hours text, and phone number. Run Playwright checks against it. Assert all three bools are `True`.
3. Create a minimal HTML page missing all checked elements. Assert all bools are `False` and `deficiencies` list is populated.
4. Simulate a timeout (non-responsive server). Assert graceful handling with `None` load time and all bools `False`.

## References

- @context/features/01-project-scaffold.md
- @context/features/03-url-discovery.md
- @context/leadscout_design_doc.md (Website Audit section)
- https://developers.google.com/speed/docs/insights/v5/get-started
- https://playwright.dev/python/docs/api/class-page
