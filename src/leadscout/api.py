import httpx

from leadscout.config import HTTP_TIMEOUT


def create_client(**kwargs) -> httpx.Client:
    """Create a configured httpx.Client for making HTTP requests.

    All HTTP calls in the project go through clients created here, ensuring
    consistent timeout and redirect behavior. Callers can override defaults
    by passing keyword arguments (e.g., a different timeout for slow endpoints).
    """
    # Start with sensible defaults, then let caller overrides win
    defaults = {
        # How long to wait for a response before giving up
        "timeout": HTTP_TIMEOUT,
        # Follow 3xx redirects automatically (common with URL shorteners and HTTPS upgrades)
        "follow_redirects": True,
    }
    # dict.update merges caller kwargs on top of defaults, so explicit args take priority
    defaults.update(kwargs)
    # Unpack the merged dict as keyword arguments to httpx.Client
    return httpx.Client(**defaults)
