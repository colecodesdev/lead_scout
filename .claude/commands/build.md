Read `context/current-feature.md` to identify the active feature. Then read the full spec file it points to in `context/features/`. Also read `coding-standards.md` for project conventions.

Implement the feature according to the spec:
1. Create all files listed in "Files to Create"
2. Modify all files listed in "Files to Modify"
3. Watch for everything in "Key Gotchas"
4. Write all tests listed in "Testing"
5. Run `uv run ruff check src/ tests/` and fix any issues
6. Run `uv run pytest` and fix any failures

Do not implement anything beyond what the current spec describes. Do not move to the next feature.
