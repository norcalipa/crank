# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The bounded organization evidence summary shared by tools and result types."""
from __future__ import annotations

from datetime import datetime
from typing import Any

#: Integer keys of an evidence summary the model and result cards may see.
EVIDENCE_SUMMARY_COUNT_KEYS = (
    "verified", "stale", "unknown", "total", "fact_coverage", "pending_review",
)


def normalize_evidence_summary(summary: Any) -> dict[str, Any] | None:
    """Project an evidence summary to its bounded shape, or ``None``.

    The summary is server-computed, but datasources are injectable and
    replies are persisted, so anything that is not exactly non-negative
    counts plus an optional ISO timestamp reads as "no summary" rather than
    reaching the model context or a result card.
    """
    if not isinstance(summary, dict):
        return None
    normalized: dict[str, Any] = {}
    for key in EVIDENCE_SUMMARY_COUNT_KEYS:
        value = summary.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        normalized[key] = value
    last_verified = summary.get("last_verified_at")
    if last_verified is not None:
        if not isinstance(last_verified, str):
            return None
        try:
            datetime.fromisoformat(last_verified)
        except ValueError:
            return None
    normalized["last_verified_at"] = last_verified
    return normalized
