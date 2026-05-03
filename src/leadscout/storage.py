import json
import logging
import tempfile
from dataclasses import asdict
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from leadscout.exceptions import StorageError
from leadscout.models import Business

logger = logging.getLogger(__name__)


class _EnumEncoder(json.JSONEncoder):
    """Custom JSON encoder for our non-stdlib-friendly types.

    dataclasses.asdict() converts nested dataclasses recursively but does
    NOT convert enum members or datetimes into JSON-friendly forms. They
    arrive at the encoder as live Python objects, and json.dumps doesn't
    know how to handle them by default. This subclass intercepts those
    types and emits the appropriate primitive (str for both).
    """

    def default(self, o):
        # StrEnum values serialize to their underlying string. .value
        # returns the raw string declared on the enum member.
        if isinstance(o, StrEnum):
            return o.value
        # datetimes serialize to ISO-8601 strings. .isoformat() produces a
        # round-trippable representation (e.g. "2026-05-03T12:34:56+00:00")
        # that datetime.fromisoformat can parse back during load.
        if isinstance(o, datetime):
            return o.isoformat()
        # Fall back to the parent's default(), which raises TypeError for
        # truly unserializable types
        return super().default(o)


def load_data(path: Path) -> list[Business]:
    """Load a list of Business objects from a JSON file.

    Returns an empty list if the file doesn't exist yet (first run).
    Raises StorageError if the file exists but can't be parsed.
    """
    if not path.exists():
        return []
    try:
        # Read the entire file as text, then parse as JSON
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
        # Reconstruct each dict back into a Business dataclass
        return [Business.from_dict(item) for item in raw]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise StorageError(f"Failed to load data from {path}: {e}") from e


def save_data(path: Path, businesses: list[Business]) -> None:
    """Save a list of Business objects to a JSON file using atomic write.

    Creates the parent directory if it doesn't exist. Uses a temp file +
    rename strategy so a crash mid-write won't leave a corrupt file.
    """
    # Ensure the data directory exists (no-op if it already does)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Convert dataclass instances to plain dicts for JSON serialization
    data = [asdict(b) for b in businesses]
    try:
        # Create a temp file in the same directory as the target. This is
        # important because rename/replace only works atomically within the
        # same filesystem.
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=".leadscout_")
        try:
            # Write JSON to the temp file. Using the fd (file descriptor)
            # returned by mkstemp so we don't leak the handle.
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, cls=_EnumEncoder, indent=2)
            # Atomic replace: on success, the target file instantly switches
            # from old content to new. Path.replace() works across platforms.
            Path(tmp_path).replace(path)
        except BaseException:
            # If anything fails (encoding error, disk full, etc.), clean up
            # the temp file so we don't leave orphan .tmp files around
            Path(tmp_path).unlink(missing_ok=True)
            raise
    except OSError as e:
        raise StorageError(f"Failed to save data to {path}: {e}") from e
    logger.info("Saved %d businesses to %s", len(businesses), path)


def merge_business(existing: Business, new: Business) -> Business:
    """Merge new data into an existing Business, updating non-empty fields.

    Used during deduplication: when we encounter a business we've already
    seen (same place_id), we update the existing record with any new non-empty
    data rather than creating a duplicate.
    """
    for fld in existing.__dataclass_fields__:
        # Never overwrite the primary key
        if fld == "place_id":
            continue
        new_val = getattr(new, fld)
        # Only update if the new value is truthy (non-empty string, non-None,
        # non-zero, non-empty list). This prevents blanking out fields that
        # the new record doesn't have data for.
        if new_val:
            setattr(existing, fld, new_val)
    return existing
