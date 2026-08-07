# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Typed outcomes for external source fetching.

Every failure that a source adapter can observe maps to an explicit, typed
error class. This is the contract that callers (future normalization,
management commands, metrics) can rely on. Errors are classified so retry
policies can distinguish:

* **transient** errors that are worth bounded retries (throttling, timeout,
  server errors);
* **permanent access/configuration** errors that must not be retried
  (unauthorized, blocked redirect/address, oversized after a valid request);
* **parse/schema** errors that indicate the source changed shape (malformed
  payload, wrong content type, schema drift).

None of these errors ever contain credentials, request bodies, or untrusted
response content.
"""

from __future__ import annotations

from typing import Optional


class SourceError(Exception):
    """Base class for all source adapter errors.

    ``category`` is one of ``"transient"``, ``"permanent"``, or ``"parse"`` and
    drives bounded retry and alerting decisions.
    """

    category = "permanent"

    def __init__(self, message: str, *, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Transient errors (bounded retries are appropriate)
# ---------------------------------------------------------------------------


class TransientSourceError(SourceError):
    """Base class for errors that may succeed on a bounded retry."""

    category = "transient"


class SourceTimeoutError(TransientSourceError):
    """The source exceeded the connect/read timeout."""


class SourceThrottledError(TransientSourceError):
    """The source returned a throttling/rate-limit response (e.g. HTTP 429).

    ``retry_after`` carries the source-provided delay in seconds when available.
    Real adapters may additionally back off for the adapter's own rate budget.
    """


class SourceServerError(TransientSourceError):
    """The source returned a 5xx server error."""


# ---------------------------------------------------------------------------
# Permanent access / configuration errors (never retried)
# ---------------------------------------------------------------------------


class UnauthorizedSourceError(SourceError):
    """Authentication or authorization failed (HTTP 401 / 403)."""


class BlockedRedirectError(SourceError):
    """A redirect pointed at a disallowed host, or redirects were exhausted."""


class BlockedAddressError(SourceError):
    """A resolved address is private, link-local, loopback, or otherwise blocked.

    Raised after DNS resolution and after resolving every redirect hop.
    """


# ---------------------------------------------------------------------------
# Parse / schema errors (source changed shape; retrying won't help)
# ---------------------------------------------------------------------------


class ParseSourceError(SourceError):
    """Base class for payloads that failed to parse or validate."""

    category = "parse"


class MalformedPayloadError(ParseSourceError):
    """The response body was not valid JSON (or was otherwise unparseable)."""


class WrongContentTypeError(ParseSourceError):
    """The response's Content-Type did not match the expected type."""


class OversizedPayloadError(ParseSourceError):
    """The response body exceeded the configured maximum before parsing."""


class SchemaDriftError(ParseSourceError):
    """The response parsed but no longer matches the expected schema."""


class RetriesExhaustedError(SourceError):
    """Bounded transient retries were exhausted.

    ``last_category`` records the category of the final underlying error.
    """

    def __init__(
        self,
        message: str,
        *,
        last_error: Optional[SourceError] = None,
    ):
        super().__init__(message)
        self.last_error = last_error
        self.last_category = getattr(last_error, "category", "transient")


__all__ = [
    "BlockedAddressError",
    "BlockedRedirectError",
    "MalformedPayloadError",
    "OversizedPayloadError",
    "ParseSourceError",
    "RetriesExhaustedError",
    "SchemaDriftError",
    "SourceError",
    "SourceServerError",
    "SourceThrottledError",
    "SourceTimeoutError",
    "TransientSourceError",
    "UnauthorizedSourceError",
    "WrongContentTypeError",
]
