# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Low-cardinality New Relic telemetry for bounded agent operations.

Telemetry is deliberately a separate boundary from application payloads.  The
allowlist below contains only operational dimensions (stage, status, source
adapter, and reason code); prompts, responses, source bodies, and arbitrary
identifiers are never accepted as event attributes.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

import newrelic.agent


EVENT_NAMES = frozenset(
    {
        "interactive_call",
        "scheduled_run",
        "source_stage",
        "matching_batch",
        "operational_change",
        "crawl_planning",
        "crawl_run_started",
        "crawl_run_completed",
        "crawl_run_failed",
        "inventory_health",
    }
)

_SAFE_KEYS = frozenset(
    {
        "eventType",
        "event_name",
        "run_type",
        "status",
        "stage",
        "source_key",
        "source_kind",
        "adapter_version",
        "reason_code",
        "capability",
        "action",
        "confirmed",
        "duration_ms",
        "latency_ms",
        "freshness_seconds",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "estimated_cost_usd",
        "items_seen",
        "items_succeeded",
        "items_failed",
        "matches_persisted",
        "sources_total",
        "sources_succeeded",
        "sources_failed",
        "users_total",
        "users_succeeded",
        "users_failed",
        "deadline_reached",
        "listings_ingested",
        "listings_updated",
        "employers_resolved",
        "employers_unresolved",
        "listings_rejected",
        "correlation_id",
        "run_id",
        "scheduled",
        "stale",
        "skipped",
        "errors",
        "organizations_total",
        "jobs_total",
        "provider",
        "model",
        "enabled_sources",
        "active_listings",
        "stale_sources",
        "repeated_failure_sources",
        "collapsed_sources",
        "unregistered_adapter_sources",
        "healthy",
    }
)
_SENSITIVE_KEY = re.compile(r"(?i)(response|body|content|secret|credential)")


def failure_reason(error: BaseException | None) -> str:
    """Map an exception to a stable, low-cardinality operational reason."""
    if error is None:
        return "none"
    name = type(error).__name__.lower()
    if "timeout" in name or isinstance(error, TimeoutError):
        return "timeout"
    if "cost" in name or "usage" in name:
        return "cost_limit"
    if "schema" in name or "validation" in name or "invalidmodel" in name:
        return "rejected"
    if "permission" in name or "auth" in name:
        return "authorization"
    if "connection" in name or "http" in name or "network" in name:
        return "upstream"
    return "internal"


def _safe_value(key: str, value: Any) -> Any:
    if key not in _SAFE_KEYS or _SENSITIVE_KEY.search(key):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if value is None:
        return None
    return str(value)[:64]


def event_attributes(event_name: str, attributes: Mapping[str, Any] | None = None) -> dict:
    """Build a bounded, redaction-safe custom-event payload."""
    if event_name not in EVENT_NAMES:
        raise ValueError(f"Unsupported monitoring event: {event_name}")
    payload = {"event_name": event_name}
    for key, value in (attributes or {}).items():
        safe = _safe_value(key, value)
        if safe is not None:
            payload[key] = safe
    return payload


def record_event(event_name: str, attributes: Mapping[str, Any] | None = None) -> None:
    """Best-effort New Relic event emission; telemetry never breaks work."""
    try:
        newrelic.agent.record_custom_event(
            "CrankOperation", event_attributes(event_name, attributes)
        )
    except Exception:  # pragma: no cover - vendor SDK defensive boundary
        return


def record_metric(name: str, value: float, attributes: Mapping[str, Any] | None = None) -> None:
    """Record a named metric with only stable dimensions."""
    try:
        newrelic.agent.record_custom_metric(name, float(value))
    except Exception:  # pragma: no cover - vendor SDK defensive boundary
        return


def capability_enabled(key: str, default: bool = True) -> bool:
    """Return an operator switch value without making startup depend on DB."""
    from crank.models.monitoring import CapabilitySwitch

    try:
        switch = CapabilitySwitch.objects.filter(key=key).only("enabled").first()
    except Exception:  # pragma: no cover - migrations/startup may precede DB
        return default
    return default if switch is None else bool(switch.enabled)


__all__ = [
    "EVENT_NAMES",
    "event_attributes",
    "failure_reason",
    "record_event",
    "record_metric",
    "capability_enabled",
]
