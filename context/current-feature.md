# Current Feature

## Active: 01 — Project Scaffold

**Spec:** `context/features/01-project-scaffold.md`

**Status:** Not started

**Goal:** Set up uv project, data models, JSON storage layer, exception hierarchy, CLI skeleton with stub subcommands, and storage unit tests.

**When done:**
- `uv run leadscout --help` prints help with all subcommands listed
- `uv run pytest tests/test_storage.py` passes
- `uv run ruff check src/ tests/` clean

**Next:** `02-places-search.md`
