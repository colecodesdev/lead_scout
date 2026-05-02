Read `context/current-feature.md` to identify the active feature.

Verify the current feature is complete:
1. Run `uv run ruff check src/ tests/` — must be clean
2. Run `uv run pytest` — must pass
3. Check that all files listed in the spec's "Files to Create" exist
4. Check that all modifications listed in "Files to Modify" are present

If anything fails, report what's incomplete. Do not advance.

If everything passes, update `context/current-feature.md` to point to the next feature spec in sequence (01 → 02 → 03 → 04 → 05). Update the status, goal, and "when done" criteria to match the new spec. Set status to "Not started".

Print a summary of what was completed and what's next.
