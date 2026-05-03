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

# --- Lead scoring weights ---
# Each key maps a deficiency to the points it adds to a business's lead score.
# Higher score = better lead (more likely to need a website or improvements).
SCORE_WEIGHTS = {
    "no_website": 100,  # No website at all: highest-value lead
    "low_performance": 30,  # PageSpeed performance score below threshold
    "no_ssl": 20,  # Site served over HTTP, not HTTPS
    "not_mobile_friendly": 15,  # Fails mobile-friendly checks
    "no_menu_page": 10,  # Restaurant has no menu page
    "no_online_ordering": 10,  # No online ordering capability
    "no_reservation_system": 5,  # No reservation/booking system
    "no_contact_info": 10,  # Missing phone/email/address on site
}
