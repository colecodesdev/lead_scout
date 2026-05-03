from dataclasses import dataclass, field
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
    """Results from auditing a business's website (PageSpeed + Playwright)."""

    performance_score: float | None = None
    accessibility_score: float | None = None
    best_practices_score: float | None = None
    is_mobile_friendly: bool | None = None
    has_ssl: bool | None = None
    load_time_ms: int | None = None
    # Feature checks done via Playwright DOM inspection
    has_menu_page: bool = False
    has_online_ordering: bool = False
    has_reservation_system: bool = False
    has_contact_info: bool = False
    # Raw API response stored for debugging and re-analysis
    raw_results: dict | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Audit":
        # Filter to only keys that match dataclass fields, so extra/unknown
        # keys in stored JSON don't cause TypeErrors on construction
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


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
    # Additional URLs found during discovery phase (besides the primary website)
    discovered_urls: list[str] = field(default_factory=list)
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

        # Only pass keys that match actual dataclass fields, ignoring any
        # unknown keys that might exist in older stored data
        filtered = {k: v for k, v in payload.items() if k in cls.__dataclass_fields__}
        return cls(
            **filtered,
            url_source=url_source,
            url_classification=url_classification,
            audit=audit,
            lead=lead,
        )
