"""Project-wide pytest configuration.

Currently only one job: prevent the CLI's .env auto-loader from
repopulating env vars that tests intentionally delete via
monkeypatch.delenv. Without this fixture, a test that deletes
GOOGLE_PLACES_API_KEY before invoking the CLI would still see it
present, because the cli() group function calls _load_env_file()
which reads the project's real .env at runtime.
"""

import pytest


@pytest.fixture(autouse=True)
def _disable_env_file_loading(monkeypatch):
    """No-op the CLI's .env loader for every test in the suite.

    Autouse=True means this applies without test functions having to
    request the fixture by name. monkeypatch is used so the patch is
    automatically undone at the end of each test.
    """
    monkeypatch.setattr("leadscout.cli._load_env_file", lambda *a, **kw: None)
