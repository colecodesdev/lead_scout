"""Tests for places_quota.PlacesQuotaTracker (feature 06).

Mirrors tests/test_discovery.py's QuotaTracker tests in coverage. The
tracker drives the cost ceiling for campaign mode, so corruption /
date-rollover paths matter as much as the happy path.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from leadscout.places_quota import QUOTA_FILENAME, PlacesQuotaTracker


def _make_tracker(
    data_dir: Path, *, safe_limit: int = 5, warn_threshold: int = 3
) -> PlacesQuotaTracker:
    # Tiny limits (safe_limit=5) so tests can drive the tracker to its
    # cap in a few iterations rather than 190.
    return PlacesQuotaTracker(
        data_dir, safe_limit=safe_limit, warn_threshold=warn_threshold,
    )


class TestConsume:
    def test_consume_increments_count_and_persists(self, tmp_path):
        q = _make_tracker(tmp_path)
        # First consume returns True (claim succeeded) and persists state.
        assert q.consume() is True
        assert q.count == 1
        # File written and parseable.
        on_disk = json.loads(
            (tmp_path / QUOTA_FILENAME).read_text(encoding="utf-8")
        )
        assert on_disk["count"] == 1
        # Stored date is today (UTC); allow either today or yesterday in
        # case we cross UTC midnight mid-test (extremely unlikely but
        # defensive).
        today = datetime.now(timezone.utc).date().isoformat()
        assert on_disk["date"] == today

    def test_consume_returns_false_at_safe_limit(self, tmp_path):
        q = _make_tracker(tmp_path, safe_limit=3)
        # Drain the budget.
        assert q.consume() is True
        assert q.consume() is True
        assert q.consume() is True
        # Next call refuses; count remains at limit.
        assert q.consume() is False
        assert q.count == 3

    def test_remaining_property(self, tmp_path):
        q = _make_tracker(tmp_path, safe_limit=4)
        assert q.remaining == 4
        q.consume()
        assert q.remaining == 3
        # Drive to zero and below — remaining clamps at 0, never negative.
        for _ in range(10):
            q.consume()
        assert q.remaining == 0


class TestPersistence:
    def test_state_persists_across_instantiations(self, tmp_path):
        q1 = _make_tracker(tmp_path)
        q1.consume()
        q1.consume()
        # New instance picks up the prior count.
        q2 = _make_tracker(tmp_path)
        assert q2.count == 2

    def test_new_utc_day_resets_count(self, tmp_path):
        # Pre-seed a state file dated yesterday with a near-limit count.
        # The tracker should ignore the count and reset to 0.
        yesterday = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).date().isoformat()
        (tmp_path / QUOTA_FILENAME).write_text(
            json.dumps({"date": yesterday, "count": 99}), encoding="utf-8",
        )
        q = _make_tracker(tmp_path)
        assert q.count == 0


class TestCorruption:
    def test_corrupt_file_defaults_to_at_limit(self, tmp_path):
        # Garbled JSON should make the tracker refuse all claims this
        # session — better to under-spend than overrun the budget.
        (tmp_path / QUOTA_FILENAME).write_text("not json", encoding="utf-8")
        q = _make_tracker(tmp_path, safe_limit=10)
        assert q.consume() is False
        assert q.count == q.safe_limit

    def test_missing_required_keys_defaults_to_at_limit(self, tmp_path):
        # Valid JSON but no "count" key. Same defensive behavior.
        (tmp_path / QUOTA_FILENAME).write_text(
            json.dumps({"date": "2026-05-09"}), encoding="utf-8",
        )
        q = _make_tracker(tmp_path, safe_limit=10)
        assert q.consume() is False


class TestNoFile:
    def test_first_run_starts_at_zero(self, tmp_path):
        # No prior file -> count starts at 0 and remaining at safe_limit.
        q = _make_tracker(tmp_path, safe_limit=7)
        assert q.count == 0
        assert q.remaining == 7
