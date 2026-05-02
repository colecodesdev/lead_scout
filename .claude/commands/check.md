Read `context/current-feature.md` to identify the active feature, then read its full spec.

Run a status check:
1. Which files from "Files to Create" exist? Which are missing?
2. Which modifications from "Files to Modify" are present?
3. `uv run ruff check src/ tests/` — any lint errors?
4. `uv run pytest` — how many pass/fail/skip?
5. Do the "When done" criteria in current-feature.md all pass?

Print a concise checklist showing what's done and what remains. Do not make any changes.
