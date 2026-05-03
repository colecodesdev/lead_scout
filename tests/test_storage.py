from datetime import datetime, timezone
from pathlib import Path

import pytest

from leadscout.exceptions import StorageError
from leadscout.models import Audit, Business, Lead, LeadTier, UrlClassification, UrlSource
from leadscout.storage import load_data, merge_business, save_data


@pytest.fixture
def data_file(tmp_path):
    # tmp_path/data exercises the auto-create-parent behavior in save_data;
    # the "data" subdir does not exist until storage creates it.
    return tmp_path / "data" / "test.json"


def _make_business(**overrides) -> Business:
    defaults = {
        "place_id": "abc123",
        "name": "Test Restaurant",
        "address": "123 Main St",
        "phone": "555-1234",
        "website": "https://example.com",
        "url_source": UrlSource.GOOGLE_PLACES,
        "url_classification": UrlClassification.OFFICIAL_SITE,
        "rating": 4.5,
        "review_count": 100,
        "business_type": "restaurant",
    }
    defaults.update(overrides)
    return Business(**defaults)


class TestSaveAndLoad:
    def test_round_trip(self, data_file):
        original = _make_business()
        save_data(data_file, [original])
        loaded = load_data(data_file)

        assert len(loaded) == 1
        b = loaded[0]
        assert b.place_id == original.place_id
        assert b.name == original.name
        assert b.address == original.address
        assert b.phone == original.phone
        assert b.website == original.website
        assert b.url_source == UrlSource.GOOGLE_PLACES
        assert b.url_classification == UrlClassification.OFFICIAL_SITE
        assert b.rating == 4.5
        assert b.review_count == 100

    def test_round_trip_last_scanned_datetime(self, data_file):
        # last_scanned was added in feature 02; the storage encoder must
        # serialize tz-aware datetimes to ISO-8601 strings, and from_dict
        # must parse them back to a tz-aware datetime equal to the original.
        scanned_at = datetime(2026, 5, 3, 12, 30, 45, tzinfo=timezone.utc)
        original = _make_business(last_scanned=scanned_at)
        save_data(data_file, [original])
        loaded = load_data(data_file)
        assert len(loaded) == 1
        assert loaded[0].last_scanned == scanned_at

    def test_round_trip_with_audit_and_lead(self, data_file):
        audit = Audit(performance_score=0.85, has_ssl=True, has_menu_page=True)
        lead = Lead(tier=LeadTier.MISSING_FEATURES, score=25, reasons=["no ordering"])
        original = _make_business(audit=audit, lead=lead)

        save_data(data_file, [original])
        loaded = load_data(data_file)

        b = loaded[0]
        assert b.audit is not None
        assert b.audit.performance_score == 0.85
        assert b.audit.has_ssl is True
        assert b.audit.has_menu_page is True
        assert b.lead is not None
        assert b.lead.tier == LeadTier.MISSING_FEATURES
        assert b.lead.score == 25
        assert b.lead.reasons == ["no ordering"]

    def test_creates_data_directory(self, data_file):
        assert not data_file.parent.exists()
        save_data(data_file, [_make_business()])
        assert data_file.parent.exists()
        assert data_file.exists()

    def test_load_nonexistent_returns_empty(self, data_file):
        result = load_data(data_file)
        assert result == []

    def test_multiple_businesses(self, data_file):
        businesses = [
            _make_business(place_id="a", name="First"),
            _make_business(place_id="b", name="Second"),
        ]
        save_data(data_file, businesses)
        loaded = load_data(data_file)
        assert len(loaded) == 2
        assert loaded[0].name == "First"
        assert loaded[1].name == "Second"


class TestMergeBusiness:
    def test_merge_updates_fields(self):
        existing = _make_business(phone="555-1111")
        new = _make_business(phone="555-2222")
        merged = merge_business(existing, new)
        assert merged.phone == "555-2222"
        assert merged.place_id == "abc123"

    def test_merge_deduplication(self, data_file):
        first = _make_business(place_id="abc", name="Original", phone="555-0000")
        save_data(data_file, [first])

        loaded = load_data(data_file)
        existing_map = {b.place_id: b for b in loaded}

        updated = _make_business(place_id="abc", name="Updated", phone="555-9999")
        new_biz = _make_business(place_id="xyz", name="Brand New")

        incoming = [updated, new_biz]
        for b in incoming:
            if b.place_id in existing_map:
                merge_business(existing_map[b.place_id], b)
            else:
                existing_map[b.place_id] = b

        result = list(existing_map.values())
        save_data(data_file, result)
        final = load_data(data_file)

        assert len(final) == 2
        by_id = {b.place_id: b for b in final}
        assert by_id["abc"].name == "Updated"
        assert by_id["abc"].phone == "555-9999"
        assert by_id["xyz"].name == "Brand New"


class TestAtomicWrite:
    def test_file_not_corrupted_on_existing_data(self, data_file):
        original = [_make_business(place_id="keep_me")]
        save_data(data_file, original)

        save_data(data_file, [_make_business(place_id="new_data")])
        loaded = load_data(data_file)
        assert len(loaded) == 1
        assert loaded[0].place_id == "new_data"

    def test_failed_write_preserves_original_and_cleans_temp(self, data_file, monkeypatch):
        # Simulate the spec's "atomic write doesn't corrupt on failure" case:
        # write a known-good file, then force the rename step to blow up
        # mid-save and verify (a) the original is untouched and (b) no
        # orphan .tmp files are left behind.
        original = [_make_business(place_id="keep_me", name="Original")]
        save_data(data_file, original)

        # Snapshot the on-disk bytes so we can confirm they're unchanged
        # even after the failed save below.
        original_bytes = data_file.read_bytes()

        # Patch Path.replace to raise. mkstemp + json.dump still run, so
        # this exercises the cleanup branch in save_data that unlinks the
        # temp file before re-raising.
        def boom(self, target):
            raise OSError("simulated failure during atomic rename")

        monkeypatch.setattr(Path, "replace", boom)

        with pytest.raises(StorageError):
            save_data(data_file, [_make_business(place_id="should_not_persist")])

        # Original file content must be byte-identical to before the failure.
        assert data_file.read_bytes() == original_bytes

        # And no .leadscout_*.tmp orphans left in the directory.
        leftovers = list(data_file.parent.glob(".leadscout_*.tmp"))
        assert leftovers == []
