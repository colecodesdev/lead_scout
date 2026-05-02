# LeadScout Design Doc

> **Status:** Draft
> **Last updated:** 2026-05-02
> **Repo:** TBD

## What & Why

A Python CLI tool that finds local restaurants without websites (or with bad ones) and scores them as freelance web design leads. Targets the 30A/Santa Rosa Beach area initially. Takes a location, searches Google Places, audits whatever web presence each business has, and outputs a ranked JSON file of leads with specific deficiencies noted. The goal is a repeatable pipeline that replaces manual Googling and produces a ready-to-work lead list for Cole Codes cold outreach.

## Scope

**In v1:**
- Search Google Places API for restaurants within a given location and radius
- Classify each result: no website, has website, third-party-only presence (Yelp/Facebook/etc.)
- Secondary web search for businesses missing a website field to catch unlinked sites
- Automated website audit via PageSpeed Insights API (Lighthouse scores) and Playwright DOM checks
- Lead scoring based on what each business is missing
- JSON output with ranked leads and per-business deficiency lists
- CLI interface with logging at every step
- Idempotent runs: skip or update previously scanned businesses, no duplicates

**Not in v1:**
- Outreach copy generation. Cole writes his own pitches using the deficiency data.
- SQLite or any database. JSON files are the storage layer.
- Web frontend or dashboard. CLI only.
- Non-restaurant business types. Restaurants first, other verticals later.
- Paid API tiers. Everything runs within free-tier limits.
- Geographic expansion beyond 30A. The tool supports arbitrary locations, but v1 is validated locally.

## Stack

- **Language:** Python 3.12+
- **HTTP client:** httpx (async-capable, timeout/retry built in)
- **Browser automation:** Playwright (headless Chromium for DOM checks)
- **Storage:** JSON files on disk (one master file per run, append/update logic)
- **CLI framework:** argparse or click (TBD during build)
- **Logging:** Python stdlib logging module
- **Hosting + CI/CD:** Local execution only. No deployment target.
- **Monitoring:** None. Logs to stdout/file.

## Data

The core data shape lives in JSON. Each run produces or updates a single file.

```
business {
  place_id          (string, unique key from Google Places)
  name              (string)
  address           (string)
  phone             (string or null)
  rating            (float or null)
  website_url       (string or null)
  url_source        (enum: "google_places" | "search_discovered" | "none")
  url_classification (enum: "official_site" | "social_media" | "directory_listing" | "none")
  last_scanned      (ISO 8601 timestamp)
}

audit {
  place_id              (string, FK to business)
  lighthouse_mobile     (object: { performance, accessibility, seo, best_practices } scores 0-100)
  lighthouse_desktop    (object: same shape)
  has_menu              (bool)
  has_hours             (bool)
  has_contact_info      (bool)
  has_mobile_viewport   (bool)
  has_ssl               (bool)
  has_online_ordering   (bool)
  has_reservation       (bool)
  load_time_seconds     (float)
  broken_assets         (list of URLs or empty)
  deficiencies          (list of strings, human-readable flags)
  audited_at            (ISO 8601 timestamp)
}

lead {
  place_id          (string, FK to business)
  score             (int, higher = better lead)
  tier              (enum: "no_website" | "failing_audit" | "missing_features" | "skip")
  deficiency_summary (list of strings)
  scored_at         (ISO 8601 timestamp)
}
```

One design choice worth noting: `url_source` and `url_classification` are separate fields because a business can have a URL discovered via secondary search (`search_discovered`) that turns out to be a directory listing (`directory_listing`), not an official site. These two axes answer different questions: where did we find it, and what is it.

## Key Decisions

**Google Custom Search API for secondary lookups instead of scraping Google search results directly.** The Custom Search JSON API has a 100 queries/day free tier. For a local scan (likely 20-60 restaurants per run), that's enough. Scraping Google Search directly violates ToS and gets IP-blocked fast. Trade-off: 100/day cap means large-area scans need to be batched across days or the secondary lookup gets skipped for overflow businesses.

**PageSpeed Insights API + Playwright instead of Playwright-only.** PageSpeed Insights runs remote Lighthouse audits for free with no browser overhead locally. Playwright adds the DOM-level checks that Lighthouse doesn't cover (menu presence, hours text, ordering links). Running both is slower per business but costs nothing extra and produces a more complete audit. If one API is down or rate-limited, the other still produces partial data.

**JSON files instead of SQLite.** For a solo CLI tool scanning tens of businesses per run, JSON is simpler to inspect, edit, diff, and version-control. SQLite adds a dependency and query layer that isn't justified until the dataset grows past what jq and Python dicts handle comfortably. Migration path to SQLite later is straightforward since the schema is already defined.

**Fuzzy string matching for secondary URL discovery instead of exact match.** When searching "{business name} {city} website", the top results might use a slightly different business name (abbreviations, missing "The", DBA vs legal name). Using a similarity threshold (e.g., rapidfuzz with a cutoff around 80) catches real matches without pulling in unrelated businesses. Trade-off: false positives are possible, but the output is a lead list for human review, not an automated action, so a few false matches are low cost.

**No robots.txt enforcement for Playwright checks.** The tool visits publicly accessible pages and checks for DOM elements (does a menu link exist, is there a phone number visible). It doesn't scrape content, follow pagination, or store page text. This is functionally equivalent to a human opening the site in a browser. robots.txt parsing adds complexity for no practical benefit at this scale.

## Dependencies & Cost

| Service | Purpose | Free tier limit |
|---|---|---|
| Google Places API (New) | Restaurant discovery by location | $200/mo credit (~5,000 Nearby Search requests) |
| Google Custom Search JSON API | Secondary URL discovery for businesses missing a website | 100 queries/day |
| PageSpeed Insights API | Remote Lighthouse audit scores | No hard cap, rate-limited ~25 req/100s |
| Playwright (local) | DOM checks on business websites | Free, runs locally |
| rapidfuzz (Python lib) | Fuzzy string matching for URL discovery | Free, MIT license |
| httpx (Python lib) | HTTP client for all API calls | Free, BSD license |

Total fixed cost: $0/mo at local scan volumes. The Google Cloud $200/mo free credit covers Places API usage for hundreds of runs. Custom Search is the real bottleneck at 100/day, but that's fine for 30A-scale scanning.

## Done

- [ ] `leadscout search --location "Santa Rosa Beach, FL" --radius 5000` returns a JSON file of discovered businesses with classifications
- [ ] Businesses without a Google Places website field get a secondary search pass and correct url_source/url_classification
- [ ] Businesses with websites get a full audit (Lighthouse scores + Playwright DOM checks) written to the same JSON
- [ ] Each business has a lead score and tier assignment
- [ ] Re-running the same search updates existing records by place_id, no duplicates
- [ ] Every API call, classification decision, and failure is logged with enough context to debug
- [ ] Runs end-to-end on a real 30A scan and produces a lead list Cole can act on
