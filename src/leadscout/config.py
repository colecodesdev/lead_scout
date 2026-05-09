# Central configuration for LeadScout.
# All tunable constants live here so pipeline modules don't have magic numbers
# scattered throughout. Import individual constants where needed.

# --- API rate-limit delays (seconds) ---
# Google Places API: 100ms between requests to stay under quota
PLACES_API_SLEEP = 0.1
# PageSpeed Insights: 4s gap keeps us under the 25 req/100s free-tier limit
PSI_API_SLEEP = 4.0

# --- HTTP retry settings (used by tenacity in api.py) ---
# Number of retry attempts for transient failures (429, 5xx)
RETRY_COUNT = 3
# Exponential backoff starts at this many seconds
RETRY_BASE_WAIT = 2
# Cap the backoff so we don't wait forever
RETRY_MAX_WAIT = 30

# --- General HTTP settings ---
# Timeout in seconds for all httpx requests
HTTP_TIMEOUT = 30

# --- Search defaults ---
# Default search radius in meters for Google Places nearby search
DEFAULT_RADIUS = 5000
# Default business type(s) for the Places Nearby Search `includedTypes` filter.
# These are Google Places "Table A" types. Pass one or more via --category on
# the CLI. Examples: "restaurant", "dentist", "doctor", "pharmacy", "gym".
DEFAULT_BUSINESS_TYPES = ["restaurant"]

# --- URL discovery & classification (feature 03) ---
# Domains that classify as social-media presence (not an owned site).
# Substring match against the URL's netloc (subdomain-tolerant).
# frozenset because it's read-only and gives O(1) membership; using it
# here makes intent obvious ("this is a fixed lookup table, not a list
# we mutate elsewhere").
SOCIAL_MEDIA_DOMAINS = frozenset({
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
})
# Third-party listing/aggregator domains. Restaurants on these are
# present-but-not-in-control: still treated as "no real website" for
# scoring purposes, just with a different reason.
DIRECTORY_DOMAINS = frozenset({
    "yelp.com",
    "tripadvisor.com",
    "grubhub.com",
    "doordash.com",
    "ubereats.com",
    "opentable.com",
    "yellowpages.com",
})
# rapidfuzz token_sort_ratio threshold (0-100). 70 is intentionally
# permissive: this is a lead list for human review, false positives are
# cheap, false negatives waste a pitch (you contact a business that
# already has a website).
FUZZY_MATCH_THRESHOLD = 70

# --- Custom Search free-tier quota (feature 03) ---
# Google Custom Search has a HARD daily cap of 100 queries on the free
# tier. No billing fallback: past 100, the API just returns 429 until
# UTC midnight. We track usage on disk and refuse to issue more queries
# than CUSTOM_SEARCH_SAFE_LIMIT in any UTC day, leaving a safety margin
# below the actual cap to absorb concurrent invocations and any drift
# between our counter and Google's.
CUSTOM_SEARCH_DAILY_LIMIT = 100  # Google's hard cap; informational only
CUSTOM_SEARCH_SAFE_LIMIT = 95  # Our hard stop; 5-query margin for safety
CUSTOM_SEARCH_WARN_THRESHOLD = 80  # Log a warning at/after this count

# --- Website audit (feature 04) ---
# Per-site Playwright timeout. 30s is enough for slow restaurant sites
# (Wix/Squarespace pages with heavy assets) without making a stalled
# scan drag forever.
AUDIT_PAGE_TIMEOUT_MS = 30_000
# After domcontentloaded fires, give the page up to this long to settle
# into "networkidle". Persistent WebSockets/analytics never settle, so
# we wrap this in try/except and proceed even if it times out.
AUDIT_NETWORKIDLE_TIMEOUT_MS = 10_000
# Mobile viewport for the headless browser. Restaurant customers are
# overwhelmingly on phones; auditing mobile behavior is the point.
AUDIT_VIEWPORT_WIDTH = 375
AUDIT_VIEWPORT_HEIGHT = 812
# Realistic Chrome UA string. Default Playwright UA is detectable and
# triggers Cloudflare/Akamai bot challenges on a non-trivial fraction
# of restaurant sites.
AUDIT_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)
# Re-audit window: skip businesses whose audit is fresher than this.
# Restaurant sites change slowly; 7 days is plenty.
AUDIT_FRESHNESS_DAYS = 7
# Lighthouse mobile-performance score below this triggers a deficiency.
# 50 is Google's "needs improvement" boundary.
AUDIT_PERFORMANCE_THRESHOLD = 50

# --- Lead scoring (feature 05) ---
# All thresholds and weights live here, NOT in scoring.py, so they can
# be tuned after the first real scan without touching application logic.
#
# Tier base scores. A business is assigned a single tier; the tier's
# base is the starting score before additive modifiers below.
# Higher base = better lead (more likely to need a website or upgrades).

# Lead tier base scores. Imported as a dict keyed by LeadTier enum values.
# Strings as keys (rather than the enum members directly) so config.py
# stays free of model imports and avoids any import-cycle risk.
LEAD_TIER_BASE_SCORES = {
    "no_website": 80,
    "failing_audit": 60,
    "missing_features": 40,
    "skip": 0,
}

# Customer-facing DOM checks. Used in two places by the scoring step:
# (a) tier assignment: 3+ failures across this set bumps a business
#     from `missing_features` up to `failing_audit`;
# (b) score modifiers: each missing element adds SCORE_PER_DOM_FAILURE
#     points.
# Mobile-viewport is intentionally excluded from this list: it's a
# developer concern, not a customer-facing feature.
SCORING_DOM_FIELDS = (
    "has_menu",
    "has_hours",
    "has_contact_info",
    "has_ssl",
    "has_online_ordering",
    "has_reservation",
)
# Failures across SCORING_DOM_FIELDS at or above this count -> failing_audit.
SCORING_DOM_FAILURE_TIER_THRESHOLD = 3
# Lighthouse mobile performance OR accessibility below this -> failing_audit.
SCORING_LIGHTHOUSE_FAIL_THRESHOLD = 50

# Score modifiers (additive). All clamped at SCORE_MAX after summing.
SCORE_NO_PRESENCE_BONUS = 10  # url_classification == none (no social/dir either)
SCORE_RATING_TIER_1_BONUS = 5  # rating >= 4.0
SCORE_RATING_TIER_2_BONUS = 5  # rating >= 4.5 (additional, total +10)
SCORE_PER_DOM_FAILURE = 2  # per missing SCORING_DOM_FIELDS element
SCORE_PERF_BAD_THRESHOLD = 30  # mobile perf below this -> bonus applied
SCORE_PERF_BAD_BONUS = 5
SCORE_SLOW_LOAD_THRESHOLD_S = 5.0  # seconds; load_time over this -> bonus
SCORE_SLOW_LOAD_BONUS = 3
SCORE_BROKEN_ASSETS_BONUS = 2  # any broken assets at all
SCORE_MAX = 100  # cap after all modifiers
