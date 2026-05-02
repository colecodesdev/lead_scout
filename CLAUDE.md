# LeadScout

Python CLI tool that finds local restaurants without websites (or with bad ones) and scores them as freelance web design leads.

## Quick Reference

- **Language:** Python 3.12+
- **Package manager:** uv
- **Entry point:** `src/leadscout/cli.py`
- **Run:** `uv run leadscout <command>`
- **Test:** `uv run pytest`
- **Lint:** `uv run ruff check src/ tests/`
- **Format:** `uv run ruff format src/ tests/`

## Project Structure

```
src/leadscout/
  cli.py          # Click CLI entry point
  models.py       # Dataclasses and enums
  storage.py      # JSON read/write
  config.py       # Constants, thresholds, rate limits
  exceptions.py   # Exception hierarchy
  api.py          # Shared httpx client factory
  search.py       # Google Places API
  discovery.py    # Custom Search API + URL classification
  audit.py        # PageSpeed Insights + Playwright DOM checks
  scoring.py      # Lead scoring and output
tests/
  fixtures/       # Sample API response JSONs
  test_*.py       # Mirror src module names
data/             # Runtime output, gitignored
```

## Build Workflow

Read `context/current-feature.md` to see what feature is active. That file points to the full spec in `context/features/`. Build order:

1. `01-project-scaffold.md` — models, storage, CLI skeleton
2. `02-places-search.md` — Google Places API integration
3. `03-url-discovery.md` — Custom Search + URL classification
4. `04-website-audit.md` — PageSpeed Insights + Playwright
5. `05-lead-scoring.md` — Scoring, ranking, output

Each feature builds on the previous. Do not skip ahead.

## Rules

- Read `coding-standards.md` before writing any code. It defines file layout, naming, error handling, logging, and testing patterns.
- Read the active spec in `context/features/` before implementing. Each spec lists exact files to create, files to modify, gotchas, and test cases.
- One function, one job. CLI handlers call service functions. Service functions do not print, they return or raise.
- All business logic errors are caught per-business. Never crash the pipeline on a single failure.
- All JSON writes go through `storage.py`. No raw `json.load/dump` anywhere else.
- All HTTP calls go through the shared `httpx.Client` in `api.py` with retry/backoff via `tenacity`.
- Log everything: what was called, what was found, what failed. Use `logging.getLogger(__name__)`.
- No print statements. Use `click.echo` for CLI output, `logging` for everything else.
- Run `uv run ruff check` and `uv run pytest` before considering any feature complete.

## Environment

Requires `.env` or exported shell vars:
```
GOOGLE_PLACES_API_KEY=
GOOGLE_CUSTOM_SEARCH_API_KEY=
GOOGLE_CUSTOM_SEARCH_CX=
```

Playwright browser install (one-time): `playwright install chromium`
