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
