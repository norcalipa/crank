# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Low-cardinality New Relic telemetry for bounded agent operations.

Telemetry is deliberately a separate boundary from application payloads.  The
allowlist below contains only operational dimensions (stage, status, source
adapter, and reason code); prompts, responses, source bodies, and arbitrary
identifiers are never accepted as event attributes.
"""

from __future__ import annotations

import functools
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
        "job_search_turn",
        "job_search_tool_invocation",
        "job_search_helpfulness_gap",
        "assistant_turn",
        "assistant_first_result",
        "availability_state",
        "preference_decision",
        "pipeline_health",
        "publication_sweep",
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
        "sources_skipped",
        "users_total",
        "users_succeeded",
        "users_failed",
        "deadline_reached",
        "listings_ingested",
        "listings_updated",
        "listings_closed",
        "listings_expired",
        "listings_deleted",
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
        "eligible",
        "skipped_policy",
        "skipped_fresh",
        "deferred_backoff",
        "deferred_budget",
        "succeeded",
        "partial",
        "failed",
        "oldest_due_age_hours",
        "sources_eligible",
        "sources_deferred",
        "oldest_source_age_hours",
        "provider",
        "model",
        "enabled_sources",
        "active_listings",
        "stale_sources",
        "repeated_failure_sources",
        "collapsed_sources",
        "unregistered_adapter_sources",
        "healthy",
        # Assistant quality guardrails (issue #397): scalar operational
        # counters only. Prompt/response text and conversation identifiers are
        # never event attributes.
        "tool",
        "result_count",
        "job_match_count",
        "organization_match_count",
        "tools_called",
        "cited_ids_count",
        "empty_result",
        "inventory_nonempty",
        "page_context",
        "actions_dropped",
        "action_drop_reasons",
        "latency_bucket",
        "provider_error_class",
        "turns_without_result",
        # Versioned job-match recompute (issue #475): CAS-publish counters.
        "stale_discarded",
        "duplicate_skipped",
        # Assistant success, data freshness and publication->match lag
        # (issue #482). Enums, ints, bools only.
        "phase",
        "state",
        "surface",
        "failure_stage",
        "decision",
        "scope",
        "origin",
        "availability_state",
        "attempt",
        "retry",
        "results",
        "cached",
        "turns",
        "seconds_to_first_result",
        "turns_to_first_result",
        "scanned",
        "processed",
        "keys_deleted",
        "outbox_oldest_age_seconds",
        "outbox_pending",
        "accepted_evidence_rows",
        "evidence_stale_rows",
        "evidence_oldest_verified_days",
        "organizations_with_evidence",
        "organizations_active",
        "observations_pending",
        "observations_conflicted",
        "field_claims_pending",
        "corrections_pending",
        "unresolved_employers",
        "queued_runs",
        "oldest_queued_age_seconds",
        "users_without_generation",
        "publication_match_lag_seconds",
        "publication_lag_max_seconds",
        "publication_lag_count",
    }
)

FAILURE_STAGES = frozenset(
    {
        "provider",
        "source",
        "publication",
        "matching",
        "availability",
        "capacity",
        "lifecycle",
        "internal",
    }
)

_FAILURE_STAGE_BY_REASON = {
    "provider_timeout": "provider",
    "cost_limit": "provider",
    "invalid_output": "provider",
    "assistant_unavailable": "availability",
    "conversation_gone": "lifecycle",
    "conversation_closed": "lifecycle",
    "preference_stale": "lifecycle",
    "preference_version_unavailable": "lifecycle",
    "service_error": "internal",
    "unexpected_error": "internal",
    "worker_interrupted": "internal",
    "rate_limited": "capacity",
    "turn_in_progress": "capacity",
    "retry_limited": "capacity",
}


def failure_stage_for(reason_code: str | None) -> str:
    """Map a turn failure/rejection reason to its finite failure stage."""
    return _FAILURE_STAGE_BY_REASON.get(reason_code or "", "internal")


# Keys whose values must come from a closed registry; a miss becomes "other".
_ENUM_KEYS = frozenset(
    {
        "status", "reason_code", "stage", "phase", "state", "surface",
        "failure_stage", "decision", "scope", "origin", "latency_bucket",
        "action", "run_type", "tool", "capability", "availability_state",
    }
)
# Keys carrying a short operator/vendor identifier (never user input).
_SLUG_KEYS = frozenset(
    {
        "source_key", "source_kind", "provider", "model",
        "provider_error_class", "adapter_version", "page_context",
    }
)
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,63}$")
_UUID = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)

_STATIC_ENUMS = {
    "status": {
        "succeeded", "failed", "skipped", "completed", "deadline", "error",
        "provider_succeeded", "provider_failed", "passed", "disabled",
        "applied", "dismissed", "undone", "stale", "invalid", "ok",
    },
    "reason_code": {
        "none", "timeout", "cost_limit", "rejected", "authorization",
        "upstream", "internal", "deadline", "overlap", "constraint",
        "overlap_lock", "overlap_existing", "overlap_constraint",
        "overlap_advisory_lock",
        "conversation_closed", "preference_stale",
        "preference_version_unavailable", "rate_limited", "turn_in_progress",
        "retry_limited", "disabled", "snapshot_failed",
    },
    "stage": {
        "job_ingest", "score_gathering", "company_profile_crawl",
        "match_recompute", "job_pipeline_matching",
    },
    "phase": {"attempted", "saved", "replied", "failed", "rejected", "replayed"},
    "surface": {"assistant_status", "job_matches"},
    "decision": {"apply", "dismiss", "undo"},
    "scope": {"account", "search"},
    "origin": {"proposal", "direct", "reset"},
    "latency_bucket": {"lt100", "100-300", "300-1000", "gt1000"},
    "action": {
        "seed_job_sources", "queue_retrieval", "queue_pipeline",
        "retry_failed", "rollback_drill", "enable", "disable",
        "approve", "block", "queue",
    },
    "capability": {"job_source", "all"},
}


@functools.lru_cache(maxsize=1)
def enum_values() -> dict:
    """Closed value registry per enum key, including dynamic sources.

    Built lazily (model imports) and cached; tests assert every literal
    ``record_event`` value is registered.
    """
    from crank import empty_state
    from crank.agents.jobs import ingest
    from crank.models.agent_run import AgentRun
    from crank.models.job_search import JobSearchTurn
    from crank.models.monitoring import ALLOWED_CAPABILITY_KEYS
    from crank.views import assistant_status

    registry = {key: set(_STATIC_ENUMS.get(key, ())) for key in _ENUM_KEYS}
    registry["failure_stage"] = set(FAILURE_STAGES)
    availability = {
        getattr(assistant_status, name)
        for name in (
            "SIGNED_OUT", "REPLIES_DISABLED", "TEMPORARILY_UNAVAILABLE",
            "REFRESHING", "INVENTORY_UNAVAILABLE", "READY",
        )
    } | {
        getattr(empty_state, name)
        for name in (
            "NO_SOURCE", "SOURCE_DISABLED", "CRAWL_RUNNING", "CRAWL_FAILED",
            "CRAWL_STALE", "CRAWL_EMPTY", "NO_PREFERENCES", "NO_MATCHES",
            "PARTIAL_COVERAGE", "OK",
        )
    }
    registry["state"] = set(availability)
    registry["availability_state"] = set(availability)
    registry["run_type"] |= set(AgentRun.RunType.values)
    registry["status"] |= set(AgentRun.Status.values)
    registry["reason_code"] |= set(JobSearchTurn.FailureCode.values)
    registry["reason_code"] |= {
        getattr(ingest, name) for name in dir(ingest) if name.startswith("SKIP_")
    }
    registry["capability"] |= set(ALLOWED_CAPABILITY_KEYS)
    registry["tool"] = {
        "query_active_organizations", "query_score_summaries",
        "search_job_listings", "get_matches_for_user",
    }
    return {key: frozenset(values) for key, values in registry.items()}


# Per-event key schemas for events introduced by issue #482; unknown keys are
# dropped. Older events keep the global allowlist so existing dashboards
# keep every attribute they query.
EVENT_SCHEMAS = {
    "assistant_turn": frozenset(
        {"phase", "attempt", "retry", "results", "latency_ms", "latency_bucket",
         "reason_code", "failure_stage", "turns"}
    ),
    "assistant_first_result": frozenset(
        {"seconds_to_first_result", "turns_to_first_result"}
    ),
    "availability_state": frozenset({"surface", "state", "cached"}),
    "preference_decision": frozenset({"decision", "scope", "status", "origin"}),
    "pipeline_health": frozenset(
        {"healthy", "reason_code", "enabled_sources", "accepted_evidence_rows",
         "evidence_stale_rows", "evidence_oldest_verified_days",
         "organizations_with_evidence", "organizations_active",
         "observations_pending", "observations_conflicted",
         "field_claims_pending", "corrections_pending", "unresolved_employers",
         "outbox_pending", "outbox_oldest_age_seconds", "queued_runs",
         "oldest_queued_age_seconds", "users_without_generation",
         "publication_match_lag_seconds"}
    ),
    "publication_sweep": frozenset(
        {"status", "scanned", "processed", "keys_deleted",
         "outbox_oldest_age_seconds", "reason_code", "failure_stage"}
    ),
}
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


def _slug(value: Any) -> str:
    text = str(value)
    return text if _SLUG.match(text) else "other"


def _safe_value(key: str, value: Any) -> Any:
    if key not in _SAFE_KEYS or _SENSITIVE_KEY.search(key) or value is None:
        return None
    if key == "run_id":
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    if key == "correlation_id":
        text = str(value)
        return text if _UUID.match(text) else None
    if key in _ENUM_KEYS:
        if isinstance(value, str) and value in enum_values().get(key, ()):
            return value
        return "other"
    if key in _SLUG_KEYS:
        return _slug(value)
    if key == "action_drop_reasons":
        parts = str(value).split(",") if value else []
        return ",".join(_slug(part) for part in parts)[:64]
    if isinstance(value, (bool, int, float)):
        return value
    return None


def event_attributes(event_name: str, attributes: Mapping[str, Any] | None = None) -> dict:
    """Build a bounded, redaction-safe custom-event payload."""
    if event_name not in EVENT_NAMES:
        raise ValueError(f"Unsupported monitoring event: {event_name}")
    payload = {"event_name": event_name}
    schema = EVENT_SCHEMAS.get(event_name)
    for key, value in (attributes or {}).items():
        if schema is not None and key not in schema:
            continue
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


def latency_bucket(ms: int) -> str:
    """Map a latency to a low-cardinality bucket for faceting."""
    ms = int(ms)
    if ms < 100:
        return "lt100"
    if ms < 300:
        return "100-300"
    if ms < 1000:
        return "300-1000"
    return "gt1000"


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
    "EVENT_SCHEMAS",
    "FAILURE_STAGES",
    "enum_values",
    "event_attributes",
    "failure_stage_for",
    "failure_reason",
    "latency_bucket",
    "record_event",
    "record_metric",
    "capability_enabled",
]
