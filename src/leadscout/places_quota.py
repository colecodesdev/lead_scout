"""Daily-cap quota tracker for the Google Places API (feature 06).

Mirrors `discovery.QuotaTracker` (which guards Custom Search) in shape and
behavior, but enforces an operator-chosen daily safe limit on Places (New)
calls. Places is the only billable surface in a campaign run, so this
tracker is the single defense between a long-running campaign and the
Maps Platform monthly bill.

State at `<data_dir>/.places_quota.json`:
    {"date": "2026-05-09", "count": 17}
The date is UTC because Google's billing cycle and quota windows are UTC,
and we want a deterministic rollover that doesn't depend on the operator's
local timezone.

Why a separate class instead of generalizing `discovery.QuotaTracker`:
two implementations is too few to justify a shared base. If a third
billable API enters the picture, factor out then. Keeping the duplication
local makes each tracker readable in isolation.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# atomic_write_text guarantees the JSON file on disk is never partially
# written: it writes to a temp file in the same dir, then renames atomically.
from leadscout.storage import atomic_write_text

logger = logging.getLogger(__name__)


# Filename inside the data dir. Leading dot keeps it adjacent to but
# visually separate from the per-location business JSON files (which
# matter to the operator) and makes glob("*.json") in the report
# command's aggregator naturally exclude it on most shells. Even so,
# the aggregator filters by leading-dot defensively.
QUOTA_FILENAME = ".places_quota.json"


class PlacesQuotaTracker:
    """Counts Places (New) calls against an operator-chosen daily cap.

    Use:
        quota = PlacesQuotaTracker(data_dir)
        if quota.consume():
            # ok to issue one Places API call
        else:
            # daily safe limit reached; the campaign loop should halt.

    Resets to 0 when the stored date no longer matches today (UTC). On
    a corrupt state file, defaults to "at limit" so we never accidentally
    overrun the budget because of an unparseable counter.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        safe_limit: int,
        warn_threshold: int,
    ) -> None:
        # `data_dir` may not exist yet on the very first run; defer
        # creation to `_persist` so a read-only `remaining` peek doesn't
        # accidentally create directories.
        self.path = data_dir / QUOTA_FILENAME
        # Limits passed in (rather than imported from config) so the
        # tests can drive the tracker with tiny limits without monkey-
        # patching module globals. Production callers pass the real
        # config constants.
        self.safe_limit = safe_limit
        self.warn_threshold = warn_threshold
        # Initialize to today, count 0; _load() will overwrite if a
        # valid prior state exists for today.
        self.date = self._today()
        self.count = 0
        self._load()

    @staticmethod
    def _today() -> str:
        """ISO date string for today in UTC. UTC matches Google's
        quota window so the local rollover lines up with theirs."""
        # datetime.now(timezone.utc) yields a tz-aware UTC datetime;
        # .date() drops the time component; .isoformat() serializes
        # to "YYYY-MM-DD". Same idiom as discovery.QuotaTracker.
        return datetime.now(timezone.utc).date().isoformat()

    def _load(self) -> None:
        """Load count from disk. Defensive on corruption."""
        today = self._today()
        # No file = first run today. Stay at count=0.
        if not self.path.exists():
            return
        try:
            # read_text returns the file contents as a string; json.loads
            # parses it. Wrap in try/except for any of the JSON / type
            # errors that mean the file is unusable.
            data = json.loads(self.path.read_text(encoding="utf-8"))
            file_date = data["date"]
            file_count = int(data["count"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            # Defensive default: assume we're at the safe limit. Better
            # to skip Places calls we could've made than to overrun
            # the budget because the counter file got corrupted.
            logger.warning(
                "Places quota file at %s is corrupt (%s). "
                "Refusing all Places calls this run; delete the file to reset.",
                self.path, e,
            )
            self.count = self.safe_limit
            return
        # Stored counter from a previous UTC day -> new day, reset.
        if file_date != today:
            logger.info(
                "Places quota date rolled over (%s -> %s); resetting count.",
                file_date, today,
            )
            self.count = 0
            self.date = today
            self._persist()
            return
        # Same UTC day as before; resume the previous count so concurrent
        # invocations on the same machine share one budget.
        self.count = file_count
        self.date = file_date

    def _persist(self) -> None:
        """Write current state to disk via atomic_write_text.

        atomic_write_text creates the parent dir as needed (verified
        in storage.py) so we don't need a separate mkdir here.
        """
        text = json.dumps({"date": self.date, "count": self.count})
        atomic_write_text(self.path, text)

    def consume(self) -> bool:
        """Try to claim one Places call against the daily budget.

        Returns True if claimed (caller may issue the request), False
        if the budget is exhausted (caller MUST skip).
        """
        if self.count >= self.safe_limit:
            return False
        self.count += 1
        self._persist()
        # Warn exactly once when crossing the threshold. Equality (==)
        # rather than >= so we don't log every subsequent claim for
        # the rest of the run.
        if self.count == self.warn_threshold:
            logger.warning(
                "Places quota at %d/%d. Campaign will halt at %d.",
                self.count, self.safe_limit, self.safe_limit,
            )
        return True

    @property
    def remaining(self) -> int:
        """How many Places calls we can still issue today."""
        return max(0, self.safe_limit - self.count)
