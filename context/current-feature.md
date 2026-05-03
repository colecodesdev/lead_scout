# Current Feature

## Status: Not Started

## Goals

## Notes

## History

- **01 — Project Scaffold** (`01-project-scaffold.md`): uv project, data models (Business, Audit, Lead), enums (UrlSource, UrlClassification, LeadTier), JSON storage with atomic writes, exception hierarchy, CLI skeleton with 5 stub subcommands, config constants, shared httpx client factory, storage unit tests (8 passing).
- **02 — Places Search** (`02-places-search.md`): Google Places (New) integration. `search_places()` geocodes a location string then walks paginated Nearby Search results (with the mandatory `next_page_token` delay). Adds `with_api_retry()` tenacity decorator factory in `api.py` (retries 429/5xx + transport errors, never auth/4xx). Adds `Business.last_scanned` (datetime) with ISO-8601 (de)serialization in storage. Wires the `search` CLI subcommand with `--location`/`--radius`, slugifies output to per-location JSON, merges via `storage.py`. 23 new tests added (32 total passing).
- **03 — URL Discovery & Classification** (`03-url-discovery.md`): `discover_urls()` runs Google Custom Search for businesses without a website, fuzzy-matches results (max of `token_sort_ratio` and `partial_ratio`, threshold 70) and prefers `official_site` over social/directory hits. Reclassifies every URL by domain afterward to correct Places-sourced misclassifications. `QuotaTracker` persists daily query count to `data/.custom_search_quota.json` (defensive on corruption, hard-stops at 95 below Google's 100/day cap). Refactor: extracted `atomic_write_text()` from `storage.save_data` so both businesses JSON and quota state share the same atomic-write mechanism. Wires the `discover` CLI subcommand with `--data-file` and `--force`. 53 new tests (85 total passing).
