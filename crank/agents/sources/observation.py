# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Immutable typed RawScoreObservation value object.

This is the typed boundary between external source payloads and normalized
application data. It is a *value object*, not a Django model: it is never
persisted directly, is frozen (immutable), and rejects malformed input at
construction time so adapters cannot leak unvalidated rows into normalization.

Validation is structural/typed (required fields, unknown fields, finite
ranges, aware/ordered timestamps, bounded confidence). Source/URL policy
(allowlist, score-type capability) is enforced separately by
:func:`crank.agents.sources.registry.validate_observation_for_source`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

_REQUIRED_TEXT_FIELDS = (
    "external_id",
    "source_url",
    "target_identity",
    "score_type",
    "adapter_version",
)
_NUMERIC_FIELDS = ("value", "min_value", "max_value")


class ObservationValidationError(ValueError):
    """Raised when a RawScoreObservation is structurally invalid."""


@dataclass(frozen=True, kw_only=True)
class RawScoreObservation:
    """A single immutable, schema-valid raw score observation.

    Fields follow the Phase 2 provenance contract (issue #311): a stable
    external identifier and source URL, the external target identity, the score
    type key, the raw and normalized value/range, observed/fetched timestamps,
    the adapter version, and run correlation for auditability.
    """

    external_id: str = field(metadata={"help": "Stable external id from the source"})
    source_url: str = field(metadata={"help": "Source URL of this observation"})
    target_identity: str = field(metadata={"help": "External identity of the rated target"})
    score_type: str = field(metadata={"help": "Score type key (see SourceCatalog capabilities)"})
    value: float = field(metadata={"help": "Normalized score value"})
    min_value: float = 0.0
    max_value: float = 5.0
    raw_value: Optional[str] = None  # sanitized raw value for audit (not a full response)
    observed_at: datetime = field(metadata={"help": "When the source recorded the score"})
    fetched_at: datetime = field(metadata={"help": "When the adapter fetched it"})
    adapter_version: str = field(metadata={"help": "Adapter implementation version"})
    run_correlation: Optional[str] = None  # correlation id linking to the owning run
    confidence: Optional[float] = None
    validation_status: Optional[str] = None  # adapter-level validation/quality marker

    def __post_init__(self) -> None:
        errors: list[str] = []

        # Required non-empty text fields.
        for name in _REQUIRED_TEXT_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{name}: required non-empty string")

        # Finite numbers.
        for name in _NUMERIC_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"{name}: must be a finite number")
            elif not math.isfinite(float(value)):
                errors.append(f"{name}: must be a finite number")

        # Timestamps: present, timezone-aware, and not inverted.
        for name in ("observed_at", "fetched_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime):
                errors.append(f"{name}: must be a datetime")
            elif value.tzinfo is None:
                errors.append(f"{name}: must be timezone-aware")

        # Range invariants (only when the basic types already pass).
        if not errors and not (self.min_value < self.max_value):
            errors.append("max_value must be strictly greater than min_value")
        if not errors and not (self.min_value <= self.value <= self.max_value):
            errors.append("value must be within [min_value, max_value]")

        if not errors and isinstance(self.observed_at, datetime) and isinstance(
            self.fetched_at, datetime
        ):
            if self.observed_at > self.fetched_at:
                errors.append("observed_at cannot be after fetched_at")

        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            errors.append("confidence must be within [0.0, 1.0]")

        if errors:
            # Frozen dataclass: raise before any object escapes construction.
            raise ObservationValidationError("; ".join(errors))


def as_observation(payload: dict[str, Any]) -> RawScoreObservation:
    """Build and validate an observation from an adapter payload dict.

    Unknown or missing keys raise ``TypeError``/``ObservationValidationError``
    respectively, keeping the adapter boundary strict and typed.
    """
    return RawScoreObservation(**payload)
