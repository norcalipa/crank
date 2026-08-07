# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Run lifecycle service for scheduled agent work.

Centralizes the database-backed overlap guard, run persistence, sanitized error
handling, structured lifecycle logs, and New Relic run events. Future scheduled
commands (score gathering, job matching) reuse this contract instead of
reimplementing idempotent run semantics.
"""
import logging
import re

import newrelic.agent
from django.utils import timezone

from crank.models.agent_run import AgentRun

logger = logging.getLogger("agent_runs")

# Keep summaries bounded and free of raw external/user data.
ERROR_SUMMARY_MAX_LENGTH = 1000

# Redact anything that looks like a credential/secret before it reaches logs or
# New Relic events.
_SECRET_PATTERNS = [
    # api keys / long hex hashes
    re.compile(r"\b[0-9a-fA-F]{32,64}\b"),
    # bearer tokens
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+"),
    # `password=...`, `api_key: ...`, etc.
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"),
]
_WHITESPACE = re.compile(r"\s+")


def sanitize_error(error, limit=ERROR_SUMMARY_MAX_LENGTH):
    """Return a bounded, secret-free, single-line summary of an exception."""
    if error is None:
        return ""
    message = str(error)
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub("<redacted>", message)
    message = _WHITESPACE.sub(" ", message).strip()
    return message[:limit]


def record_agent_event(run, event_type, **fields):
    """Emit a New Relic custom event describing a run lifecycle transition.

    Never includes raw credentials or untrusted external/user data. The call is
    best-effort: observability must never break the run itself.
    """
    try:
        newrelic.agent.record_custom_event(
            "AgentRun",
            {
                "eventType": event_type,
                "run_type": run.run_type,
                "status": run.status,
                "correlation_id": str(run.correlation_id),
                "run_id": run.pk,
                **fields,
            },
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to record New Relic AgentRun event")


def claim_run(run_type):
    """Atomically claim the scheduler slot for ``run_type``.

    Returns the claimed ``AgentRun``. Raises ``IntegrityError`` if another run
    of this type is already running; the caller should record that invocation
    as skipped (see :func:`record_skipped`).
    """
    run = AgentRun.objects.create(
        run_type=run_type,
        status=AgentRun.Status.RUNNING,
        started_at=timezone.now(),
    )
    logger.info(
        "agent run claimed: run_type=%s status=%s correlation_id=%s",
        run.run_type,
        run.status,
        run.correlation_id,
    )
    record_agent_event(
        run,
        "run_started",
        started_at=run.started_at.isoformat() if run.started_at else None,
    )
    return run


def record_skipped(run_type):
    """Record a skipped invocation for a run type that is already active."""
    run = AgentRun.objects.create(
        run_type=run_type,
        status=AgentRun.Status.SKIPPED,
        started_at=timezone.now(),
        finished_at=timezone.now(),
    )
    logger.info(
        "agent run skipped (overlap): run_type=%s correlation_id=%s",
        run.run_type,
        run.correlation_id,
    )
    record_agent_event(run, "run_skipped")
    return run


def finalize_success(run, counts=None):
    """Mark a run as succeeded with its outcome counters."""
    run.finalize(AgentRun.Status.SUCCEEDED, counts=counts or {})
    logger.info(
        "agent run succeeded: run_type=%s correlation_id=%s counts=%s",
        run.run_type,
        run.correlation_id,
        run.counts,
    )
    record_agent_event(run, "run_succeeded", counts=run.counts)
    return run


def finalize_failure(run, error, counts=None):
    """Mark a run as failed with a sanitized error summary and propagate."""
    summary = sanitize_error(error)
    run.finalize(AgentRun.Status.FAILED, counts=counts, error_summary=summary)
    logger.error(
        "agent run failed: run_type=%s correlation_id=%s error_summary=%s",
        run.run_type,
        run.correlation_id,
        summary,
    )
    record_agent_event(run, "run_failed", error_summary=summary)
    return run