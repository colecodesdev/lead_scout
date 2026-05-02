# Coding Standards

## Language

- Python 3.12+ required. Use modern syntax: `match` statements, `type` aliases, `|` union types.
- Type hints on all function signatures. Use `typing` imports only when stdlib alternatives don't exist (e.g., `dict[str, Any]` not `Dict[str, Any]`).
- Prefer `dataclasses` with `@dataclass` for structured data. No Pydantic, no attrs.
- Use `Enum` or `StrEnum` for fixed-value fields (tiers, classifications, url sources).
- f-strings for all string formatting. No `.format()`, no `%`.

## Framework

- No framework. This is a CLI tool.
- `click` for CLI argument parsing and subcommands. One `@click.group()` entry point in `cli.py`, one `@click.command()` per pipeline stage.
- All business logic lives outside of CLI handlers. CLI functions parse args, call a service function, handle output. Nothing else.

## Styling

- Not applicable. No UI.

## File Organization

- Package root: `src/leadscout/`
- CLI entry point: `src/leadscout/cli.py`
- Pipeline stages: `src/leadscout/search.py`, `src/leadscout/discovery.py`, `src/leadscout/audit.py`, `src/leadscout/scoring.py`
- Data models: `src/leadscout/models.py`
- JSON storage: `src/leadscout/storage.py`
- HTTP/API utilities: `src/leadscout/api.py`
- Config and constants: `src/leadscout/config.py`
- Tests: `tests/` (mirrors src layout, e.g., `tests/test_search.py`)
- Output data: `data/` (gitignored, created at runtime)

## Naming

- Modules: `snake_case.py`
- Functions: `snake_case`
- Classes and dataclasses: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Enums: `PascalCase` class, `UPPER_SNAKE_CASE` members
- Private helpers: `_prefixed_snake_case`

## Database

- No database. JSON files in `data/` directory.
- One file per run output: `data/leads_{location}_{timestamp}.json`
- One cumulative file per location: `data/{location_slug}.json` (append/update by `place_id`)
- All JSON read/write goes through `storage.py`. No raw `json.load()`/`json.dump()` in pipeline modules.
- Always write via temp file + atomic rename (`pathlib.Path.rename()`) to avoid partial writes.

## Data Fetching

- `httpx.Client` (sync) for all HTTP calls. One shared client instance per pipeline run with a base timeout of 30 seconds.
- Retry with exponential backoff for 429 and 5xx responses. Use `tenacity` library: 3 retries, base wait 2 seconds, max wait 30 seconds.
- Respect rate limits: sleep between batched API calls. Google Places: 100ms between requests. PageSpeed Insights: 4 second gap (stays under 25 req/100s). Custom Search: no batching needed at 100/day volume.
- Validate all API responses before processing. Check HTTP status, check for expected keys. Log and skip on unexpected shape, don't crash.

## Error Handling

- Never crash the full pipeline on a single business failure. Catch per-business exceptions, log them, continue to the next.
- Use structured return values: functions return the data directly on success, raise typed exceptions on failure.
- Define a small exception hierarchy in `src/leadscout/exceptions.py`: `LeadScoutError` base, `APIError`, `AuditError`, `StorageError`.
- Log every exception with business name/place_id context so failures are traceable.

## Logging

- Use Python stdlib `logging` module. Configure once in `cli.py`.
- Default level: `INFO` to stdout. `--verbose` flag sets `DEBUG`.
- Every API call logs: what was called, the target, and whether it succeeded or failed.
- Every classification and scoring decision logs: what was decided and why.
- Use `logging.getLogger(__name__)` in each module. No print statements.

## Testing

- **Framework:** pytest
- **Scope:** Unit tests for scoring logic, classification logic, and storage read/write. Integration tests for API response parsing using fixture data (no live API calls in tests).
- **File pattern:** `tests/test_{module}.py`
- **Run:** `uv run pytest` (single run) or `uv run pytest --watch` with pytest-watch (watch mode)
- **Fixtures:** Store sample API responses as JSON files in `tests/fixtures/`. Load with `pathlib.Path` in test setup.

## Code Quality

- No commented-out code unless explicitly requested.
- No unused imports or variables.
- Keep functions under 50 lines when possible.
- One function, one job. If a function does search + classify + score, split it.
- No nested functions deeper than one level. Extract to module-level helpers.
- `ruff` for linting and formatting. Config in `pyproject.toml`.
