from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

# --- Enums ---
# Using StrEnum (Python 3.11+) so enum values serialize directly as strings
# in JSON without needing .value calls everywhere. Each member's value matches
# the string used in storage and API communication.


class UrlSource(StrEnum):
    """Where a business's URL was found."""

    GOOGLE_PLACES = "google_places"  # Came from Google Places API response
    SEARCH_DISCOVERED = "search_discovered"  # Found via Google Custom Search
    NONE = "none"  # No URL found yet


class UrlClassification(StrEnum):
    """What kind of site a URL points to."""

    OFFICIAL_SITE = "official_site"  # The business's own website
    SOCIAL_MEDIA = "social_media"  # Facebook, Instagram, etc.
    DIRECTORY_LISTING = "directory_listing"  # Yelp, TripAdvisor, etc.
    NONE = "none"  # Not yet classified


class LeadTier(StrEnum):
    """How valuable a business is as a potential web design lead."""

    NO_WEBSITE = "no_website"  # Best lead: has no website at all
    FAILING_AUDIT = "failing_audit"  # Good lead: website exists but fails quality checks
    MISSING_FEATURES = "missing_features"  # Decent lead: website works but lacks key features
    SKIP = "skip"  # Not a lead: website is fine


# --- Data models ---
# Using @dataclass instead of plain dicts so we get type safety, IDE
# autocompletion, and a clear schema. Each model has a from_dict classmethod
# for deserializing from JSON.


@dataclass
class Audit:
    """Results from auditing a business's website.

    Populated by feature 04 (audit). Two layers feed this:

    1. PageSpeed Insights (remote Lighthouse): runs once for `mobile`
       and once for `desktop`; each populates a dict of category scores
       (performance / accessibility / seo / best_practices, 0-100).
    2. Playwright DOM checks (local headless browser at iPhone viewport):
       boolean checks for menu/hours/contact/viewport/SSL/online-ordering/
       reservations, plus load timing and broken-asset collection.

    `deficiencies` is a list of plain-English strings derived from the
    bool/score fields; it's what the scoring step (feature 05) consumes
    rather than re-deriving from the raw fields.
    """

    # PageSpeed Insights (Lighthouse) results, keyed by strategy.
    # Each dict has shape: {"performance": 0-100, "accessibility": 0-100,
    # "seo": 0-100, "best_practices": 0-100}. None if PSI failed.
    lighthouse_mobile: dict | None = None
    lighthouse_desktop: dict | None = None

    # Playwright DOM checks. All default False so a partial audit
    # (e.g., site blocked by Cloudflare) still serializes cleanly.
    has_menu: bool = False
    has_hours: bool = False
    has_contact_info: bool = False
    has_mobile_viewport: bool = False
    has_ssl: bool = False
    has_online_ordering: bool = False
    has_reservation: bool = False

    # Wall-clock seconds from page.goto() start to networkidle.
    # None on timeout / Cloudflare block / any navigation failure.
    load_time_seconds: float | None = None

    # Asset requests that returned 4xx/5xx during page load.
    # Each entry: {"url": str, "status": int, "type": "image"|"script"|"stylesheet"|"other"}.
    # Captured via page.on("response") listener registered before goto.
    broken_assets: list[dict] = field(default_factory=list)

    # Plain-English deficiencies produced by the audit. Stable strings
    # so feature 05 can match on them ("No menu page found", "Mobile
    # performance score: 23/100", "No SSL certificate", "3 broken images").
    deficiencies: list[str] = field(default_factory=list)

    # When the audit ran (UTC). Used by the 7-day skip window.
    # Same shape and serialization as Business.last_scanned.
    audited_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Audit":
        # Shallow copy so the caller's dict isn't mutated by the pop()s
        # below. Same defensive pattern as Business.from_dict.
        payload = dict(data)
        # Parse audited_at (ISO-8601 string -> tz-aware datetime).
        # Pop so it doesn't collide with the explicit kwarg.
        scanned_raw = payload.pop("audited_at", None)
        audited_at = (
            datetime.fromisoformat(scanned_raw) if scanned_raw else None
        )
        # Filter to only known fields so unknown JSON keys don't trip
        # the cls(**...) call. Same defensive pattern Business uses.
        filtered = {k: v for k, v in payload.items() if k in cls.__dataclass_fields__}
        return cls(**filtered, audited_at=audited_at)


@dataclass
class Lead:
    """Scoring result for a business as a potential lead."""

    tier: LeadTier = LeadTier.SKIP
    score: int = 0
    # Human-readable list of why this business scored the way it did
    reasons: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Lead":
        return cls(
            # Reconstruct the enum from its stored string value
            tier=LeadTier(data["tier"]),
            score=data.get("score", 0),
            reasons=data.get("reasons", []),
        )


@dataclass
class Business:
    """A single restaurant/business discovered during the search phase.

    This is the central data model. It accumulates data as it moves through
    the pipeline: search fills basic info, discovery adds URLs, audit adds
    quality metrics, and scoring adds the final lead tier.
    """

    # place_id is the unique identifier from Google Places API, used for dedup
    place_id: str
    name: str
    address: str = ""
    phone: str = ""
    website: str = ""
    url_source: UrlSource = UrlSource.NONE
    url_classification: UrlClassification = UrlClassification.NONE
    rating: float | None = None
    review_count: int = 0
    business_type: str = ""
    # Google Places (New) `businessStatus`: "OPERATIONAL",
    # "CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY", or "" when the API
    # didn't return it. Empty default keeps prior saved JSON readable
    # via from_dict's filter (missing key -> dataclass default).
    # Used by feature 06 (campaign mode) to optionally filter out
    # closed listings before they consume discovery quota.
    business_status: str = ""
    # Additional URLs found during discovery phase (besides the primary website)
    discovered_urls: list[str] = field(default_factory=list)
    # When this record was last fetched/refreshed from the source API.
    # Used by future re-run logic to skip recently-scanned businesses
    # and to flag stale data. Stored as a tz-aware datetime; serialized
    # as an ISO-8601 string by the storage encoder.
    last_scanned: datetime | None = None
    # These get populated in later pipeline stages
    audit: Audit | None = None
    lead: Lead | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Business":
        # Work on a shallow copy so we can pop keys without mutating the
        # caller's dict (json.loads gives us a fresh dict per call, but
        # tests and future callers may reuse the same dict).
        payload = dict(data)

        # Pop nested objects before constructing, since they need their own
        # from_dict deserialization and shouldn't end up in **filtered below.
        audit_data = payload.pop("audit", None)
        lead_data = payload.pop("lead", None)

        # Reconstruct nested dataclasses if present in the stored data
        audit = Audit.from_dict(audit_data) if audit_data else None
        lead = Lead.from_dict(lead_data) if lead_data else None

        # Reconstruct enums from their string values. Pop them so they
        # don't conflict with the explicit keyword args below.
        url_source = UrlSource(payload.pop("url_source", "none"))
        url_classification = UrlClassification(payload.pop("url_classification", "none"))

        # last_scanned is serialized as an ISO-8601 string by the storage
        # encoder. Parse it back to a tz-aware datetime here so downstream
        # code gets a real datetime, not a string. Pop it so it doesn't
        # collide with the keyword arg below.
        # datetime.fromisoformat handles the "+00:00" / "Z" suffix from
        # .isoformat() output (Python 3.11+ accepts "Z" too).
        scanned_raw = payload.pop("last_scanned", None)
        last_scanned = datetime.fromisoformat(scanned_raw) if scanned_raw else None

        # Only pass keys that match actual dataclass fields, ignoring any
        # unknown keys that might exist in older stored data
        filtered = {k: v for k, v in payload.items() if k in cls.__dataclass_fields__}
        return cls(
            **filtered,
            url_source=url_source,
            url_classification=url_classification,
            last_scanned=last_scanned,
            audit=audit,
            lead=lead,
        )
