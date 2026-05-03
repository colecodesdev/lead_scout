import json
import logging
import re
import tempfile
from dataclasses import asdict
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from leadscout.exceptions import StorageError
from leadscout.models import Business

logger = logging.getLogger(__name__)


# Slug regex: any run of non-word characters becomes a single underscore.
# Compiled once at module load. Used by data_path_for_location below.
_LOCATION_SLUG_RE = re.compile(r"[^\w]+")


def data_path_for_location(data_dir: Path, location: str) -> Path:
    """Resolve the per-location JSON file path under `data_dir`.

    Slugifies the location string for use as a filename: lowercases,
    replaces runs of non-word characters with single underscores, and
    strips trailing underscores. Used by the `search` subcommand to
    write the file and by `run` to thread it through the pipeline.

    Example:
        data_path_for_location(Path("./data"), "Santa Rosa Beach, FL")
        -> Path("./data/santa_rosa_beach_fl.json")
    """
    slug = _LOCATION_SLUG_RE.sub("_", location.lower()).strip("_")
    return data_dir / f"{slug}.json"


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


def atomic_write_text(path: Path, text: str) -> None:
    """Write text to a file atomically (temp file + rename strategy).

    Public helper used wherever we need to write a small text file
    without risking a half-written file on crash. Creates the parent
    directory if it doesn't already exist.

    Used by save_data (for the businesses JSON) and by feature 03's
    QuotaTracker (for the per-day Custom Search counter).
    """
    # Ensure the destination directory exists. parents=True creates any
    # missing intermediate dirs; exist_ok=True makes this a no-op when
    # the dir is already there.
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # mkstemp creates a temp file IN THE SAME DIRECTORY as the target.
        # This is crucial: Path.replace() (the atomic-rename step below)
        # only works atomically on the same filesystem, and tmp_path on a
        # different mount would silently degrade to a non-atomic copy.
        fd, tmp_path = tempfile.mkstemp(
            dir=path.parent, suffix=".tmp", prefix=".leadscout_"
        )
        try:
            # Wrap the file descriptor returned by mkstemp in a normal
            # `with open(fd, ...)` so the handle is closed even on error.
            # If we used the path with `open(tmp_path, "w")` the fd would
            # leak.
            with open(fd, "w", encoding="utf-8") as f:
                f.write(text)
            # Atomic replace: on POSIX this is rename(2); on Windows
            # MoveFileExW with MOVEFILE_REPLACE_EXISTING. Either way, the
            # target file instantly flips from old content to new.
            Path(tmp_path).replace(path)
        except BaseException:
            # Anything goes wrong (write error, disk full, KeyboardInterrupt),
            # clean up the temp file so we don't leave .leadscout_*.tmp
            # orphans. missing_ok=True so unlink doesn't raise if Path.replace
            # already moved the file (race-free cleanup).
            Path(tmp_path).unlink(missing_ok=True)
            raise
    except OSError as e:
        # Only OSErrors get wrapped; other exceptions (e.g. JSON encoder
        # errors at the call site) propagate as themselves.
        raise StorageError(f"Failed to atomically write {path}: {e}") from e


def save_data(path: Path, businesses: list[Business]) -> None:
    """Save a list of Business objects to a JSON file using atomic write."""
    # Convert dataclass instances to plain dicts. asdict recurses into
    # nested dataclasses (Audit, Lead) and tuples them out as dicts.
    data = [asdict(b) for b in businesses]
    # Serialize first, then hand the text to atomic_write_text. Doing
    # the serialization outside the helper means encoder errors raise
    # cleanly as TypeError (or similar) rather than being wrapped in
    # StorageError, which keeps debugging direct.
    text = json.dumps(data, cls=_EnumEncoder, indent=2)
    atomic_write_text(path, text)
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
