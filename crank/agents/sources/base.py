# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Typed score-adapter contract for Phase 2 (issue #311).

Defines the exceptions, the immutable :class:`RawScoreObservation` value
object, the :class:`SourceAdapter` protocol, and the base-domain allowlist
validation shared by every source adapter and the adapter factory. Concrete
network fetching (issue #311 out of scope) is deliberately not implemented
here; adapters import this protocol and return validated observations.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from crank.agents.sources.allowlist import is_domain_allowed
from crank.agents.sources.observation import (  # noqa: F401
    RawScoreObservation,
    ObservationValidationError,
)

__all__ = [
    "RawScoreObservation",
    "ObservationValidationError",
    "SourceCatalogError",
    "UnknownSourceAdapter",
    "SourceNotApproved",
    "SourceBlocked",
    "SourceDisabled",
    "UnapprovedBaseUrl",
    "SourceAdapter",
    "validate_source_base_url",
]


class SourceCatalogError(Exception):
    """Base class for source-catalog/adapter lifecycle errors."""


class UnknownSourceAdapter(SourceCatalogError):
    """Raised when a ``SourceCatalog.adapter_key`` has no registered adapter."""


class SourceNotApproved(SourceCatalogError):
    """Raised when a source is not yet in the ``approved`` approval state."""


class SourceBlocked(SourceCatalogError):
    """Raised when a source is in the ``blocked`` approval state."""


class SourceDisabled(SourceCatalogError):
    """Raised when a source is approved but not operator-enabled."""


class UnapprovedBaseUrl(SourceCatalogError):
    """Raised when a source's base URL is not on the code-owned allowlist."""


def validate_source_base_url(url: str) -> None:
    """Validate that ``url`` is HTTPS on a code-owned allowlisted domain.

    Rejects non-HTTPS schemes, URLs embedding credentials, and hosts that are
    not on (or a subdomain of) :data:`APPROVED_SOURCE_DOMAINS`. Raises
    :class:`UnapprovedBaseUrl` on any violation; returns ``None`` when valid.
    """
    value = (url or "").strip()
    if not value.startswith("https://"):
        raise UnapprovedBaseUrl(
            f"Source base URL must be HTTPS: {value!r}"[:200]
        )
    try:
        remainder = value[len("https://"):]
        host = remainder.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        # Reject userinfo (no credentials in URLs from the database).
        if "@" in host:
            raise UnapprovedBaseUrl(
                "Source base URL must not embed credentials"
            )
    except UnapprovedBaseUrl:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize malformed URLs
        raise UnapprovedBaseUrl(f"Malformed source base URL: {exc}") from exc

    if not is_domain_allowed(host):
        raise UnapprovedBaseUrl(
            f"Source base URL host {host!r} is not on the approval allowlist"
        )


@runtime_checkable
class SourceAdapter(Protocol):
    """Contract every source adapter must satisfy.

    ``key``/``version`` are class attributes that identify the recorded
    implementation; ``fetch`` performs the (out-of-scope, concrete) retrieval
    and must return only schema-valid :class:`RawScoreObservation` values.
    """

    key: str
    version: str

    def fetch(self, source) -> list[RawScoreObservation]:  # pragma: no cover
        """Return validated observations for ``source`` (concrete impl)."""
        ...
