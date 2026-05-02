# Project Scaffold Spec

## Overview

Set up the LeadScout project structure, data models, JSON storage layer, and CLI skeleton. This is the foundation everything else plugs into. Lives at `src/leadscout/`. No route or access constraints; this is a local CLI tool.

## Requirements

- Initialize a `uv` project with `pyproject.toml` at repo root
- Dependencies: `click`, `httpx`, `tenacity`, `rapidfuzz`, `ruff`, `pytest`
- Dev dependency: `pytest-watch`
- Playwright installed separately via `playwright install chromium`
- Create package structure per `coding-standards.md` file organization section
- Define all data models in `models.py` as `@dataclass` classes: `Business`, `Audit`, `Lead`
- Define enums: `UrlSource` ("google_places", "search_discovered", "none"), `UrlClassification` ("official_site", "social_media", "directory_listing", "none"), `LeadTier` ("no_website", "failing_audit", "missing_features", "skip")
- `storage.py` implements: `load_data(path) -> list[Business]`, `save_data(path, businesses)`, `merge_business(existing, new) -> Business` (update without duplicating by `place_id`)
- All JSON writes use temp file + atomic rename
- `storage.py` handles serialization of dataclasses to/from JSON (custom encoder/decoder using `dataclasses.asdict` and a `from_dict` classmethod on each model)
- `cli.py` sets up a `click.group()` with a `--verbose` flag and `--data-dir` option (default: `./data`)
- Stub subcommands registered but empty: `search`, `discover`, `audit`, `score`, `run` (full pipeline)
- Logging configured in CLI entry point: INFO default, DEBUG on `--verbose`
- `config.py` holds all constants: API rate limit sleeps, retry counts, default radius, score weights
- `exceptions.py` defines: `LeadScoutError`, `APIError`, `AuditError`, `StorageError`
- `.gitignore` includes `data/`, `.env`, `__pycache__/`, `.ruff_cache/`
- `ruff` configured in `pyproject.toml`: line length 100, target Python 3.12
- Unit tests for storage: round-trip save/load, merge deduplication by place_id, atomic write doesn't corrupt on simulated failure

## Files to Create

1. `pyproject.toml`: Project config, dependencies, ruff settings
2. `src/leadscout/__init__.py`: Package init
3. `src/leadscout/cli.py`: Click group, logging setup, stub subcommands
4. `src/leadscout/models.py`: Business, Audit, Lead dataclasses and enums
5. `src/leadscout/storage.py`: JSON read/write/merge logic
6. `src/leadscout/config.py`: Constants and defaults
7. `src/leadscout/exceptions.py`: Exception hierarchy
8. `src/leadscout/api.py`: Shared httpx client factory with timeout/retry config
9. `tests/__init__.py`: Empty
10. `tests/test_storage.py`: Storage unit tests
11. `tests/fixtures/`: Directory for sample JSON data
12. `.gitignore`: Standard Python + project-specific ignores
13. `.env.example`: Template for required API keys

## Key Gotchas

- `uv` uses `pyproject.toml` natively. No `setup.py` or `setup.cfg`. The `[project.scripts]` table defines the CLI entry point: `leadscout = "leadscout.cli:cli"`.
- `uv` expects `src/` layout by default when using `uv init --lib`. Make sure the `[tool.uv.sources]` or build system config points at `src/`.
- `dataclasses.asdict()` handles nested dataclasses but does NOT handle `Enum` members cleanly; they come out as raw values. The custom JSON encoder needs to call `.value` on enum fields. Conversely, `from_dict` must reconstruct enums from strings.
- `click.group()` with `invoke_without_command=True` lets `leadscout` alone print help. Each subcommand is a separate `@cli.command()`.

## Environment Variables

```
GOOGLE_PLACES_API_KEY=
GOOGLE_CUSTOM_SEARCH_API_KEY=
GOOGLE_CUSTOM_SEARCH_CX=
```

## Notes

- The `run` subcommand will chain search -> discover -> audit -> score in sequence once all features are built. For now it's a stub that prints "not yet implemented."
- `data/` directory is created at runtime by `storage.py` if it doesn't exist. Not committed to git.
- No `.env` loader (like python-dotenv) as a dependency. Use `os.environ.get()` directly. Keys can be exported in shell or set in `.env` loaded by the user's shell profile.

## Testing

1. Create a `Business` with known fields, save to JSON, load back, assert equality
2. Save two businesses, save a modified version of the first, load and confirm only two entries exist with the updated fields
3. Confirm `data/` directory is auto-created if missing

## References

- @context/leadscout_design_doc.md (What & Why, Data, Stack sections)
- @coding-standards.md (file organization, naming, error handling)
- https://click.palletsprojects.com/en/stable/
- https://docs.astral.sh/uv/
