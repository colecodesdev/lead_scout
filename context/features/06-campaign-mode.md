# Campaign Mode Spec

## Overview

Extend LeadScout from "one location, one category at a time" to a multi-day, multi-location, multi-category campaign mode. A user defines a TOML plan listing locations and Places categories. A new `campaign` subcommand processes one day's slice of jobs (cron-friendly via Windows Task Scheduler), filtering out dead listings and named franchise chains before they consume API quota. A new `report` subcommand aggregates ranked leads across every per-location JSON in the data directory into one combined markdown.

Cost protection: Google Places (New) is the only billable surface (Custom Search is not in use; PageSpeed Insights is free and shares the Places key). A new `PlacesQuotaTracker` mirrors the existing `QuotaTracker` from `discovery.py` and hard-stops the campaign at a configurable daily safe limit.

## Requirements

- New module `src/leadscout/filtering.py` with `filter_live_businesses(businesses, *, min_review_count=1, blocked_chain_names=None) -> tuple[list[Business], list[Business]]`. Pure function, returns `(kept, dropped)`. Drops if `review_count < min_review_count` or normalized name (lowercased, punctuation stripped) contains any entry from `blocked_chain_names`.
- New module `src/leadscout/pipeline.py` with `run_pipeline(location, radius, categories, data_dir, *, places_key, cs_key, cs_cx, psi_key, force=False, min_review_count=1, blocked_chain_names=None) -> list[Business]`. Body lifted from `cli.run`'s try-block; inserts `filter_live_businesses` between search and discover. The existing `cli.run` is rewired to call this so behavior is preserved.
- New module `src/leadscout/places_quota.py` with `PlacesQuotaTracker` class mirroring `discovery.QuotaTracker`. State at `<data_dir>/.places_quota.json`. `consume()` returns `False` at safe limit; `remaining` property exposed. Wired into `_search_nearby` so each paginated request increments the counter; raises `APIError("Places daily safe limit reached")` mid-pagination if exceeded.
- New module `src/leadscout/campaign.py`:
  - `JobSpec` dataclass: `location: str`, `category: str`, `radius: int`.
  - `CampaignSummary` dataclass: counts of jobs (total/run/skipped_fresh/remaining), `halted_reason: str | None`, filtered counts (dead/chain).
  - `load_plan(path: Path) -> tuple[list[JobSpec], dict]` parses TOML via stdlib `tomllib`, cross-products locations × categories, validates against `KNOWN_BUSINESS_TYPES` (logs warning on unknown but still runs).
  - `select_todays_jobs(jobs, data_dir, *, refresh_days=7) -> tuple[list[JobSpec], list[JobSpec]]` returns `(todo, skipped_fresh)`. A `(location, category)` job is fresh when every business in that location's JSON with matching `business_type` has `last_scanned >= now - refresh_days`.
  - `run_campaign(plan_path, data_dir, *, max_jobs=None, dry_run=False) -> CampaignSummary` orchestrates. Checks `PlacesQuotaTracker` before each job; halts gracefully when its safe limit is reached.
- Add `business_status: str = ""` field to `Business`. Parse `place.get("businessStatus", "")` in `search.py::_parse_place`. Add `places.businessStatus` to `FIELD_MASK`.
- Additions to `src/leadscout/scoring.py`:
  - `aggregate_leads(data_dir: Path) -> list[Business]`: globs `*.json` under `data_dir`, ignores hidden files (`.places_quota.json`, `.custom_search_quota.json`), loads each via `load_data`, returns combined list. No dedup (a chain franchise in two cities = two `place_id`s = two records).
  - `export_campaign_markdown(businesses, path, *, top_n=50, min_tier=LeadTier.MISSING_FEATURES)`: cross-location markdown. Adds 'Location' column derived from address tail. Per-location summary table at top.
- Additions to `src/leadscout/config.py`:
  - `PLACES_DAILY_LIMIT = 200` (~$5.76/month worst-case at $0.032/call, well under $200 free credit).
  - `PLACES_SAFE_LIMIT = 190` (10-call margin).
  - `PLACES_WARN_THRESHOLD = 150`.
  - `CAMPAIGN_MIN_REVIEW_COUNT = 1`.
  - `CAMPAIGN_REFRESH_DAYS = 7`.
  - `KNOWN_BUSINESS_TYPES`: curated frozenset of ~30 high-yield Places "Table A" types (restaurant, cafe, dentist, plumber, salon, etc.).
  - `BLOCKED_CHAIN_NAMES`: frozenset of normalized franchise name substrings (mcdonald, subway, starbucks, etc.).
- Wire two new CLI subcommands in `src/leadscout/cli.py`:
  - `leadscout campaign --plan PATH [--max-jobs N] [--dry-run]`: calls `run_campaign`, prints `CampaignSummary`.
  - `leadscout report [--top N] [--min-tier T] [--output PATH]`: calls `aggregate_leads` + `export_campaign_markdown`.
- Plan file format: TOML with `[defaults]` table (`radius`, `min_review_count`) and one or more `[[jobs]]` arrays each with `location: str` and `categories: list[str]`. Cross-product expansion produces one `JobSpec` per `(location, category)` pair.

## Files to Create

1. `src/leadscout/filtering.py`: dead-business + chain filter.
2. `src/leadscout/places_quota.py`: `PlacesQuotaTracker`.
3. `src/leadscout/pipeline.py`: extracted `run_pipeline`.
4. `src/leadscout/campaign.py`: `JobSpec`, `CampaignSummary`, `load_plan`, `select_todays_jobs`, `run_campaign`.
5. `tests/test_filtering.py`
6. `tests/test_places_quota.py`
7. `tests/test_pipeline.py`
8. `tests/test_campaign.py`
9. `tests/fixtures/campaign_minimal.toml`

## Files to Modify

1. `src/leadscout/config.py`: new constants, `KNOWN_BUSINESS_TYPES`, `BLOCKED_CHAIN_NAMES`.
2. `src/leadscout/models.py`: add `business_status` field; teach `from_dict` to read it.
3. `src/leadscout/search.py`: parse `businessStatus`, add to `FIELD_MASK`, wire `PlacesQuotaTracker.consume()` into `_search_nearby`.
4. `src/leadscout/scoring.py`: `aggregate_leads` + `export_campaign_markdown`.
5. `src/leadscout/cli.py`: rewire `run` to call `pipeline.run_pipeline`, add `campaign` and `report` subcommands.
6. `tests/test_cli.py`: campaign and report command tests, regression for `run`.
7. `tests/test_scoring.py`: aggregate + export tests.
8. `tests/test_search.py`: `business_status` field-mask + parsing.

## Key Gotchas

- `merge_business` (storage.py) overwrites only with truthy values. A business previously enriched with `review_count = 12` won't be clobbered by a fresh API response showing `review_count = 0`. That's intentional and the right behavior — document in `filtering.py`'s docstring.
- The chain block-list does **substring** matching against a normalized name (lowercased, punctuation stripped). `"mcdonald"` matches `"McDonald's #4521"` and `"McDonalds Express"`. Avoid entries that would false-positive (e.g., don't add `"BP"` for British Petroleum because it would match every "BP Auto Parts").
- `KNOWN_BUSINESS_TYPES` is a soft validator: unknown categories log a warning but still run. Hard rejection would force a code edit every time the user wants to try a new Places type.
- The Places quota tracker uses UTC midnight as the rollover boundary (matches Google's billing cycle and the existing Custom Search tracker).
- TOML parsing requires Python 3.11+ (stdlib `tomllib`). Project already pins 3.12+, so this is a non-issue.
- The `report` command must skip hidden files in its glob (`.places_quota.json`, `.custom_search_quota.json`). Glob `*.json` doesn't match `.x.json` on most platforms, but be defensive — exclude any filename starting with `.` explicitly.
- `pipeline.run_pipeline` should preserve the existing soft-fail for Custom Search (cli.py:476-480 logic). Campaign mode inherits this for free if the lift is faithful.
- When `aggregate_leads` loads multiple location JSONs, it does **not** dedup by `place_id`. A franchise location in Destin and one in Santa Rosa Beach are two distinct `place_id`s and represent two distinct sales prospects.

## Test Cases

### `tests/test_filtering.py`
- Drops zero-review businesses; keeps `review_count >= min`.
- `min_review_count=0` is a no-op.
- Chain match is substring against normalized name.
- Punctuation in name doesn't defeat the chain match (`"McDonald's #4521"` → `mcdonalds 4521` → matches `mcdonald`).
- Returns kept and dropped lists separately, both populated.

### `tests/test_places_quota.py`
- `consume()` increments count, persists to disk.
- `consume()` returns `False` at safe limit.
- New UTC day resets the count.
- State persists across instantiations.
- Corrupted state file: defensive recovery (start at 0, don't crash) — mirrors `QuotaTracker` behavior.

### `tests/test_pipeline.py`
- `run_pipeline` calls `filter_live_businesses` after search and before discover.
- Default filter drops zero-review entries.
- Soft-fails Custom Search to `reclassify_urls` when `cs_key` or `cs_cx` is `None`.
- Returns the post-score list (last stage's output).

### `tests/test_campaign.py`
- `load_plan` cross-products locations × categories.
- `load_plan` warns (logs) on unknown category but doesn't drop it.
- `[defaults]` section propagates `radius` to each `JobSpec`.
- `select_todays_jobs`: fresh combos excluded, stale included, never-scanned included.
- `run_campaign` halts on Places quota mid-loop, returns summary with `halted_reason`.
- `--dry-run` invokes neither pipeline nor quota consume.
- Summary tallies dead-filtered and chain-filtered counts.

### `tests/test_cli.py` additions
- `campaign` command parses `--plan`, calls `run_campaign`.
- `--dry-run` propagates.
- `report` command writes a markdown across multiple location files.
- `run` command still works after the pipeline extraction (regression).

### `tests/test_scoring.py` additions
- `aggregate_leads` loads every `*.json` under `data_dir`.
- Hidden files (`.places_quota.json`) are excluded.
- `export_campaign_markdown` filters by `min_tier` and respects `top_n`.

### Manual smoke
1. Write `campaign.toml` with 2 small locations × 2 categories.
2. `uv run leadscout campaign --plan campaign.toml --data-dir ./data --dry-run` — prints job specs + quota state, no API calls.
3. Drop `--dry-run`, run for real. Inspect `data/*.json`: every business has `review_count >= 1`, no business name matches a `BLOCKED_CHAIN_NAMES` entry.
4. `uv run leadscout report --data-dir ./data --top 20` — cross-location markdown produced.
5. Re-run the same campaign next day: log line `Skipping <location>/<category>: fresh within 7 days` for every job. `.places_quota.json` count barely moves.
