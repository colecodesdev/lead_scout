import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from leadscout.config import (
    HTTP_TIMEOUT,
    RETRY_BASE_WAIT,
    RETRY_COUNT,
    RETRY_MAX_WAIT,
)

logger = logging.getLogger(__name__)


# HTTP status codes that indicate a *transient* server-side problem worth
# retrying. 429 = "too many requests" (rate-limited; back off and try again).
# 5xx = upstream is sick. We deliberately exclude 4xx auth/validation errors
# (401/403/404/422) because retrying them just wastes time and quota: the
# request is wrong, repeating it won't change the answer.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


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


def _is_retryable_exception(exc: BaseException) -> bool:
    """Decide whether an exception thrown by an httpx call should be retried.

    Used as the predicate for tenacity's retry_if_exception. Returns True
    only for transient failures: network blips and the explicitly-listed
    retryable HTTP status codes. Everything else (auth failures, 404s,
    JSON decode errors, etc.) re-raises immediately so callers see the
    real problem instead of waiting through pointless retries.
    """
    # httpx.TransportError covers connection failures, DNS errors, timeouts,
    # and protocol errors. These are almost always worth a retry.
    if isinstance(exc, httpx.TransportError):
        return True
    # raise_for_status() converts non-2xx responses into HTTPStatusError.
    # We only retry the codes in our allowlist above.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    # Anything else is treated as a permanent failure.
    return False


def with_api_retry(
    *,
    attempts: int | None = None,
    base_wait: float | None = None,
    max_wait: float | None = None,
):
    """Return a tenacity retry decorator preconfigured for our HTTP calls.

    Centralizes retry policy so every API caller in the project gets
    consistent behavior. Defaults come from config.py constants; callers
    can override per-call (e.g., feature 04's PageSpeed audit may want
    longer waits because the API is slower).

    Used as a decorator on functions that perform a single HTTP request:

        @with_api_retry()
        def _fetch_page(client, ...): ...

    Notes:
    - reraise=True so callers see the underlying httpx exception on the
      final failure, not tenacity's RetryError wrapper. That keeps our
      try/except blocks in business modules straightforward.
    - wait_exponential uses multiplier * 2^(attempt-1), capped at `max`.
      With defaults (multiplier=2, max=30), waits go 2s, 4s, 8s, 16s, 30s.
    """
    # Resolve each parameter against its config-default. Using `or` would be
    # subtly wrong here (a caller passing 0 would silently fall through to
    # the default), so we use explicit `is None` checks.
    attempts = RETRY_COUNT if attempts is None else attempts
    base_wait = RETRY_BASE_WAIT if base_wait is None else base_wait
    max_wait = RETRY_MAX_WAIT if max_wait is None else max_wait

    return retry(
        # stop_after_attempt(N) means N total tries, not N retries after
        # the first. After the Nth failure, tenacity gives up.
        stop=stop_after_attempt(attempts),
        # Exponential backoff with a ceiling. multiplier seeds the growth;
        # max prevents the wait from ballooning if the upstream stays sick.
        wait=wait_exponential(multiplier=base_wait, max=max_wait),
        # Custom predicate from above: only retry transient failures.
        retry=retry_if_exception(_is_retryable_exception),
        # Re-raise the original exception on the last attempt instead of
        # wrapping in tenacity.RetryError; lets callers catch httpx errors directly.
        reraise=True,
    )
