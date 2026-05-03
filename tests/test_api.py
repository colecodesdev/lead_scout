"""Tests for src/leadscout/api.py.

These tests pin down the retry policy independent of any caller. The
search.py tests verify that _search_nearby correctly translates retry
exhaustion into APIError; the tests here verify the underlying decision
of "what counts as retryable" so future API callers (discovery, audit)
inherit the policy with confidence.
"""

import httpx
import pytest

from leadscout.api import with_api_retry


@pytest.fixture
def no_sleep(monkeypatch):
    """Skip tenacity's exponential-backoff sleeps so retry tests run instantly."""
    # Patching the time module's sleep catches both our code and tenacity's
    # internal nap.sleep, which is just `import time; time.sleep(...)`.
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Construct an HTTPStatusError as if response.raise_for_status() was called.

    HTTPStatusError requires a Request and Response on construction; the
    cheapest way to fabricate one in a test is to build them by hand.
    """
    request = httpx.Request("GET", "http://example.test")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response
    )


class TestWithApiRetry:
    def test_retries_on_429_then_succeeds(self, no_sleep):
        # Mutable counter so the inner closure can observe its own call count.
        # A bare int wouldn't be reassignable from the closure without `nonlocal`.
        calls = {"n": 0}

        @with_api_retry()
        def fn():
            calls["n"] += 1
            # Fail twice with 429, then return a sentinel value on attempt 3.
            if calls["n"] < 3:
                raise _make_http_status_error(429)
            return "ok"

        assert fn() == "ok"
        assert calls["n"] == 3

    def test_retries_on_5xx(self, no_sleep):
        # Spot-check 503 to confirm at least one 5xx code is retried; the
        # full retryable set is documented in api.RETRYABLE_STATUS_CODES.
        calls = {"n": 0}

        @with_api_retry()
        def fn():
            calls["n"] += 1
            if calls["n"] < 2:
                raise _make_http_status_error(503)
            return "ok"

        assert fn() == "ok"
        assert calls["n"] == 2

    def test_retries_on_transport_error(self, no_sleep):
        # httpx.ConnectError is a subclass of TransportError; transient
        # network failures should always be retried.
        calls = {"n": 0}

        @with_api_retry()
        def fn():
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ConnectError("connection refused")
            return "ok"

        assert fn() == "ok"
        assert calls["n"] == 2

    def test_does_not_retry_on_401(self, no_sleep):
        # Auth failures are permanent; retrying them just wastes the
        # caller's time and the API quota. The decorator should give up
        # after the very first attempt.
        calls = {"n": 0}

        @with_api_retry()
        def fn():
            calls["n"] += 1
            raise _make_http_status_error(401)

        with pytest.raises(httpx.HTTPStatusError):
            fn()
        assert calls["n"] == 1

    def test_does_not_retry_on_404(self, no_sleep):
        # 404 means the resource doesn't exist; same logic as 401.
        calls = {"n": 0}

        @with_api_retry()
        def fn():
            calls["n"] += 1
            raise _make_http_status_error(404)

        with pytest.raises(httpx.HTTPStatusError):
            fn()
        assert calls["n"] == 1

    def test_does_not_retry_on_non_httpx_exception(self, no_sleep):
        # The predicate only knows about httpx exceptions. A bare ValueError
        # (or any other non-httpx error) propagates unchanged on first try
        # so genuine bugs surface fast instead of repeating N times.
        calls = {"n": 0}

        @with_api_retry()
        def fn():
            calls["n"] += 1
            raise ValueError("programmer error")

        with pytest.raises(ValueError):
            fn()
        assert calls["n"] == 1

    def test_exhausts_retries_and_reraises_real_exception(self, no_sleep):
        # reraise=True means callers see the original httpx error on the
        # final attempt, not tenacity.RetryError. This keeps the catch-and-
        # rewrap pattern in business modules straightforward.
        calls = {"n": 0}

        @with_api_retry()
        def fn():
            calls["n"] += 1
            raise _make_http_status_error(429)

        with pytest.raises(httpx.HTTPStatusError):
            fn()
        # Default RETRY_COUNT is 3 attempts total.
        assert calls["n"] == 3

    def test_attempts_override(self, no_sleep):
        # Passing attempts=2 should cap the retry budget to two total tries.
        # Tests that callers can tighten or loosen the policy per-call.
        calls = {"n": 0}

        @with_api_retry(attempts=2)
        def fn():
            calls["n"] += 1
            raise _make_http_status_error(429)

        with pytest.raises(httpx.HTTPStatusError):
            fn()
        assert calls["n"] == 2
