# Exception hierarchy for LeadScout.
# All custom exceptions inherit from LeadScoutError so callers can catch the
# base class to handle any project-specific error, or catch a subclass for
# more granular control.


class LeadScoutError(Exception):
    """Base exception for all LeadScout errors."""

    pass


class APIError(LeadScoutError):
    """Raised when an external API call fails (HTTP errors, unexpected responses)."""

    pass


class AuditError(LeadScoutError):
    """Raised when a website audit fails (PageSpeed, Playwright issues)."""

    pass


class StorageError(LeadScoutError):
    """Raised when JSON read/write operations fail (corrupt files, I/O errors)."""

    pass
