# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Immutable typed raw-score observation and adapter protocol.

This module defines the boundary between external source payloads and the rest
of the application. A :class:`RawScoreObservation` is the shared raw
observation contract that every source adapter must produce, along with adapter
and version metadata (issue #311). It deliberately stops short of normalization
into :class:`crank.models.Score`; no score normalization, persistence, or
scheduling lives here (out of scope for the vertical slice).

Instances are created only through :meth:`RawScoreObservation.create` so unknown
fields, missing required fields, invalid ranges, and invalid timestamps are
rejected up front.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Dict, Optional, Protocol, Sequence

from crank.agents.sources.errors import SchemaDriftError


@dataclass(frozen=True)
class RawScoreObservation:
    """A single, validated raw observation parsed from an external source.

    Attributes
    ----------
    external_id:
        Primary identifier assigned by the external source.
    source_url:
        Canonical URL of the source record/entity.
    target_identity:
        Identity of the scored target as reported by the source (may be a name
        or external id the source exposes; normalization maps it later).
    score_type:
        Type of score reported (source-native, not yet normalized).
    value:
        The numeric score value.
    range_low / range_high:
        Inclusive range the score is reported against (e.g. 1.0..5.0).
    observed_at:
        Timestamp the source reported the value to be current (source clock).
    fetched_at:
        Timestamp this adapter fetched the value (our clock).
    adapter:
        Adapter key that produced this observation.
    adapter_version:
        Version of the adapter implementation (metadata for provenance).
    run_correlation_id:
        Optional correlation id shared across a run's logs/metrics.
    """

    external_id: str
    source_url: str
    target_identity: str
    score_type: str
    value: float
    range_low: float
    range_high: float
    observed_at: datetime
    fetched_at: datetime
    adapter: str
    adapter_version: str
    run_correlation_id: Optional[str] = None

    @classmethod
    def create(
        cls,
        *,
        external_id: str,
        source_url: str,
        target_identity: str,
        score_type: str,
        value: float,
        range_low: float,
        range_high: float,
        observed_at: datetime,
        fetched_at: datetime,
        adapter: str,
        adapter_version: str,
        run_correlation_id: Optional[str] = None,
    ) -> "RawScoreObservation":
        """Validate inputs and return an immutable :class:`RawScoreObservation`.

        Rejects missing/empty identifiers, an inverted or non-finite range, a
        value outside the range, and naive/invalid timestamps. Unknown keyword
        arguments are rejected because the class is frozen (a ``TypeError`` is
        raised automatically for them).
        """
        _require_nonempty("external_id", external_id)
        _require_nonempty("source_url", source_url)
        _require_nonempty("target_identity", target_identity)
        _require_nonempty("score_type", score_type)
        _require_nonempty("adapter", adapter)
        _require_nonempty("adapter_version", adapter_version)

        if run_correlation_id is not None and not run_correlation_id.strip():
            raise SchemaDriftError("run_correlation_id must be non-empty when provided")

        if not (_isfinite(range_low) and _isfinite(range_high)):
            raise SchemaDriftError("score range bounds must be finite")
        if range_low > range_high:
            raise SchemaDriftError(
                "score range low must not exceed high "
                f"(got {range_low!r}..{range_high!r})"
            )
        if not _isfinite(value):
            raise SchemaDriftError("score value must be finite")
        if value < range_low or value > range_high:
            raise SchemaDriftError(
                f"score value {value!r} outside range {range_low!r}..{range_high!r}"
            )

        _require_aware("observed_at", observed_at)
        _require_aware("fetched_at", fetched_at)

        return cls(
            external_id=external_id,
            source_url=source_url,
            target_identity=target_identity,
            score_type=score_type,
            value=value,
            range_low=range_low,
            range_high=range_high,
            observed_at=observed_at,
            fetched_at=fetched_at,
            adapter=adapter,
            adapter_version=adapter_version,
            run_correlation_id=run_correlation_id,
        )


@dataclass(frozen=True)
class SourceQuery:
    """A validated, bounded query for a source adapter.

    Fields are adapter-meaningful; the vertical slice's Yelp adapter uses
    ``term``/``location`` plus pagination caps. All fields are optional so the
    contract stays source-agnostic while remaining typed.
    """

    term: Optional[str] = None
    location: Optional[str] = None
    max_observations: Optional[int] = None
    max_pages: Optional[int] = None


@dataclass(frozen=True)
class SourceResult:
    """The outcome of a source fetch: typed observations plus counts.

    The vertical slice does not persist anything; ``observations`` is the only
    contract handoff. Counts help future run records answer "how many".
    """

    observations: Sequence[RawScoreObservation]
    pages_fetched: int = 0
    items_seen: int = 0


class SourceAdapter(Protocol):
    """Protocol for a source adapter (issue #311).

    Adapters must be registered under a stable key and return typed
    ``RawScoreObservation`` objects. Authentication, pagination, timeouts,
    limits, rate-limit handling, and bounded retries are the adapter's
    responsibility, all routed through ``SafeHTTPClient``.
    """

    key: str
    version: str

    def fetch(self, query: SourceQuery) -> SourceResult:
        """Fetch and parse raw observations for ``query``."""
        ...


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_nonempty(name: str, value: Any) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise SchemaDriftError(f"{name} must be a non-empty value")


def _isfinite(value: float) -> bool:
    return isinstance(value, (int, float)) and value == value and value not in (
        float("inf"),
        float("-inf"),
    )


def _require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise SchemaDriftError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise SchemaDriftError(f"{name} must be timezone-aware")


def observation_to_dict(observation: RawScoreObservation) -> Dict[str, Any]:
    """Serializable dict for provenance/metadata (no raw payloads)."""
    return {
        field.name: (
            value.isoformat()
            if isinstance(value, datetime)
            else value
        )
        for field in fields(observation)
        for value in [getattr(observation, field.name)]
    }


__all__ = [
    "RawScoreObservation",
    "SourceAdapter",
    "SourceQuery",
    "SourceResult",
    "observation_to_dict",
]
