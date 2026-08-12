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
from datetime import timedelta

import newrelic.agent
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from crank.models.agent_run import AgentRun
from crank.services import monitoring

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
    attributes = {
        "eventType": event_type,
        "run_type": run.run_type,
        "status": run.status,
        "correlation_id": str(run.correlation_id),
        "run_id": run.pk,
    }
    if run.started_at and run.finished_at:
        attributes["duration_ms"] = int(
            (run.finished_at - run.started_at).total_seconds() * 1000
        )
    # Only stable scalar counters are allowed into telemetry.  The legacy
    # New Relic event remains for compatibility with existing dashboards.
    for key, value in fields.items():
        if key == "counts" and isinstance(value, dict):
            attributes.update(
                {
                    k: v
                    for k, v in value.items()
                    if k in monitoring._SAFE_KEYS
                    and isinstance(v, (int, float, bool))
                }
            )
        elif key in {"started_at", "completed_at"}:
            continue
        elif key in monitoring._SAFE_KEYS:
            attributes[key] = value
    try:
        newrelic.agent.record_custom_event("AgentRun", attributes)
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to record New Relic AgentRun event")
    operation_status = {
        "run_started": "running",
        "run_succeeded": "succeeded",
        "run_failed": "failed",
        "run_skipped": "skipped",
    }.get(event_type, run.status)
    operation = {
        key: value
        for key, value in attributes.items()
        if key not in {"eventType", "status"}
    }
    operation.update({"run_type": run.run_type, "status": operation_status})
    # Keep the existing run_started event as the low-volume claim marker;
    # terminal events carry the scheduled-run operation telemetry. This avoids
    # duplicating the same New Relic call while preserving legacy consumers.
    if event_type != "run_started":
        monitoring.record_event("scheduled_run", operation)


def claim_run(run_type):
    """Atomically claim the scheduler slot for ``run_type``.

    Returns the claimed ``AgentRun``. Raises ``IntegrityError`` if another run
    of this type is currently running; the caller should record that
    invocation as skipped (see :func:`record_skipped`).

    The overlap guard is DB-portable. The partial unique constraint
    (``unique_agentrun_running_per_type``) is the authoritative guard on
    databases that support partial indexes (SQLite, Postgres). MySQL does not
    support partial indexes, so we ALSO serialize claims in the service layer:
    concurrent claims lock the most recent non-skipped row for the run type and
    re-check for an existing RUNNING row before inserting. A RUNNING claim
    older than ``AGENT_RUN_STALE_AFTER_SECONDS`` is treated as a crashed/stale
    lock and reclaimed (finalized as failed) before a new claim is allowed.
    """
    stale_after = timedelta(seconds=int(
        getattr(settings, "AGENT_RUN_STALE_AFTER_SECONDS", 3600)
    ))
    with transaction.atomic():
        # Serialize concurrent claims for the same run type. Locking the most
        # recent non-skipped row (when one exists) makes contenders block on a
        # single row, so the re-check below is race-free on DBs without partial
        # indexes. (The residual first-ever-insert window for a brand-new run
        # type is covered by the partial unique constraint where supported.)
        AgentRun.objects.filter(
            run_type=run_type
        ).exclude(
            status=AgentRun.Status.SKIPPED
        ).order_by("-id").select_for_update().first()

        now = timezone.now()
        active = AgentRun.objects.filter(
            run_type=run_type, status=AgentRun.Status.RUNNING
        ).first()
        if active is not None:
            if active.started_at and (now - active.started_at) >= stale_after:
                # Crashed/stale lock: claimed but never finalized (e.g. a pod
                # died between claim and finalize). Reclaim so the run type is
                # not blocked forever.
                active.finalize(
                    AgentRun.Status.FAILED,
                    error_summary=(
                        "Stale run reclaimed: started but never finalized before "
                        "the staleness TTL (possible crash)."
                    ),
                )
            else:
                raise IntegrityError(
                    f"Agent run {run_type} is already running"
                )

        run = AgentRun.objects.create(
            run_type=run_type,
            status=AgentRun.Status.RUNNING,
            started_at=now,
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
    if run.started_at and run.finished_at:
        monitoring.record_metric(
            "Crank/AgentRun/DurationMs",
            (run.finished_at - run.started_at).total_seconds() * 1000,
            {"run_type": run.run_type, "status": run.status},
        )
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
    monitoring.record_event(
        "scheduled_run",
        {
            "run_type": run.run_type,
            "status": run.status,
            "reason_code": monitoring.failure_reason(error),
        },
    )
    logger.error(
        "agent run failed: run_type=%s correlation_id=%s error_summary=%s",
        run.run_type,
        run.correlation_id,
        summary,
    )
    record_agent_event(run, "run_failed", error_summary=summary)
    return run
