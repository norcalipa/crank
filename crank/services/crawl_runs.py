# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Safe, auditable single-source crawl execution."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import re
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from crank.agents.jobs.base import JobSourceQuery
from crank.agents.jobs.ingest import ingest_jobs
from crank.models.agent_run import AgentRun
from crank.models.crawl_run import CrawlRun
from crank.models.job import JobSourceCatalog
from crank.models.monitoring import OperationalChangeAudit
from crank.models.source import ApprovalState, SourceCatalog
from crank.services import agent_runs, monitoring
from crank.services.company_crawler import crawl_company_profile

SOURCE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_LISTINGS = 100
MAX_PAGES = 10


class CrawlRequestError(ValueError):
    """Raised when a crawl request cannot safely be accepted."""


def _validate_key(source_key: str) -> str:
    if not isinstance(source_key, str) or not SOURCE_KEY_RE.fullmatch(source_key.strip()):
        raise CrawlRequestError("source key is invalid")
    return source_key.strip()


def _resolve(model, source_key: str):
    key = _validate_key(source_key)
    query = Q(name=key) | Q(adapter_key=key)
    if key.isdigit():
        query |= Q(pk=int(key))
    matches = list(model.objects.filter(query)[:2])
    if not matches:
        raise CrawlRequestError("source key was not found")
    if len(matches) > 1:
        raise CrawlRequestError("source key is ambiguous")
    return matches[0]


def resolve_source(source_key: str, source_type: str):
    if source_type == CrawlRun.SourceType.ORGANIZATION:
        return _resolve(SourceCatalog, source_key)
    if source_type == CrawlRun.SourceType.JOB:
        return _resolve(JobSourceCatalog, source_key)
    raise CrawlRequestError("source type must be organization or job")


def _policy_check(source, source_type: str) -> None:
    if source_type == CrawlRun.SourceType.ORGANIZATION:
        approved = ApprovalState.APPROVED
    else:
        approved = JobSourceCatalog.ApprovalState.APPROVED
    if source.approval_state != approved:
        raise CrawlRequestError("source is not approved")
    if not source.enabled:
        raise CrawlRequestError("source is disabled")
    if not str(source.adapter_key).strip():
        raise CrawlRequestError("source adapter is not configured")


def _safe_counts(result: Any) -> dict[str, int | float | bool]:
    if is_dataclass(result):
        values = asdict(result)
    else:
        values = dict(getattr(result, "counts", {}) or {})
    aliases = {
        "ingested": "listings_ingested",
        "updated": "listings_updated",
        "observations": "items_succeeded",
        "errors": "items_failed",
        "pages_fetched": "pages_fetched",
    }
    safe: dict[str, int | float | bool] = {}
    for key, value in values.items():
        destination = aliases.get(key, key)
        if destination in monitoring._SAFE_KEYS and isinstance(value, (bool, int, float)):
            safe[str(destination)] = value
    if hasattr(result, "total") and isinstance(result.total, int):
        safe.setdefault("items_seen", result.total)
    return safe


def _error_summary(result: Any) -> str:
    reasons = getattr(result, "error_reasons", ()) or ()
    if reasons:
        return ", ".join(str(reason)[:120] for reason in reasons[:3])[:500]
    return agent_runs.sanitize_error(getattr(result, "error_summary", ""), limit=500)


def _is_timeout(result: Any) -> bool:
    text = _error_summary(result).lower()
    return "timeout" in text or "timed out" in text


def _execute(source, source_type: str):
    if source_type == CrawlRun.SourceType.ORGANIZATION:
        return crawl_company_profile(source)
    query = JobSourceQuery(
        max_listings=max(1, min(int(getattr(settings, "CRAWL_MAX_LISTINGS", MAX_LISTINGS)), MAX_LISTINGS)),
        max_pages=max(1, min(int(getattr(settings, "CRAWL_MAX_PAGES", MAX_PAGES)), MAX_PAGES)),
    )
    return ingest_jobs(source, query)


def _outcome(result: Any) -> str:
    counts = _safe_counts(result)
    errors = int(getattr(result, "errors", 0) or 0)
    completed = int(counts.get("observations", 0) or 0) + int(counts.get("items_seen", 0) or 0)
    if _is_timeout(result):
        return CrawlRun.Outcome.TIMEOUT
    if errors and completed:
        return CrawlRun.Outcome.PARTIAL
    if errors:
        return CrawlRun.Outcome.FAILURE
    return CrawlRun.Outcome.SUCCESS


def trigger_crawl(*, source_key: str, source_type: str, requested_by=None) -> CrawlRun:
    """Execute one bounded crawl and persist its complete lifecycle.

    The provider result is reduced to allowlisted counters immediately. No
    response body, credential, or provider payload is retained or emitted.
    """
    source = resolve_source(source_key, source_type)
    _policy_check(source, source_type)
    canonical_key = str(source.adapter_key)[:64]
    try:
        with transaction.atomic():
            agent_run = AgentRun.objects.create(
                run_type=AgentRun.RunType.CRAWL,
                status=AgentRun.Status.RUNNING,
                started_at=timezone.now(),
            )
            fields = {"source_type": source_type, "source_key": canonical_key}
            if source_type == CrawlRun.SourceType.ORGANIZATION:
                fields["source"] = source
            else:
                fields["job_source"] = source
            run = CrawlRun.objects.create(
                **fields,
                requested_by=requested_by if requested_by and requested_by.is_authenticated else None,
                agent_run=agent_run,
                started_at=agent_run.started_at,
                outcome=CrawlRun.Outcome.RUNNING,
            )
            OperationalChangeAudit.record(
                actor=requested_by,
                target_type=f"{source_type}_source",
                target_id=canonical_key,
                action="crawl_triggered",
                new_value={"run_id": run.pk, "source_key": canonical_key},
                confirmed=True,
            )
    except IntegrityError as exc:
        raise CrawlRequestError("a crawl for this source is already running") from exc

    monitoring.record_event(
        "crawl_run_started",
        {"run_id": run.pk, "source_key": canonical_key},
    )
    try:
        result = _execute(source, source_type)
        outcome = _outcome(result)
        counts = _safe_counts(result)
        summary = _error_summary(result)
        run.outcome = outcome
        run.counts = counts
        run.error_summary = summary
        run.finished_at = timezone.now()
        run.save(update_fields=["outcome", "counts", "error_summary", "finished_at", "modified"])
        agent_run.finalize(
            AgentRun.Status.SUCCEEDED if outcome in {CrawlRun.Outcome.SUCCESS, CrawlRun.Outcome.PARTIAL} else AgentRun.Status.FAILED,
            counts=counts,
            error_summary=summary,
        )
        event = "crawl_run_completed" if outcome in {CrawlRun.Outcome.SUCCESS, CrawlRun.Outcome.PARTIAL} else "crawl_run_failed"
        monitoring.record_event(event, {"run_id": run.pk, "source_key": canonical_key})
        OperationalChangeAudit.record(
            actor=requested_by,
            target_type=f"{source_type}_source",
            target_id=canonical_key,
            action="crawl_completed" if event == "crawl_run_completed" else "crawl_failed",
            new_value={"run_id": run.pk, "outcome": outcome},
            confirmed=True,
        )
        return run
    except Exception as exc:
        summary = f"{type(exc).__name__} ({monitoring.failure_reason(exc)})"
        outcome = CrawlRun.Outcome.TIMEOUT if monitoring.failure_reason(exc) == "timeout" else CrawlRun.Outcome.FAILURE
        run.outcome = outcome
        run.error_summary = summary
        run.finished_at = timezone.now()
        run.save(update_fields=["outcome", "error_summary", "finished_at", "modified"])
        agent_run.finalize(AgentRun.Status.FAILED, error_summary=summary)
        monitoring.record_event("crawl_run_failed", {"run_id": run.pk, "source_key": canonical_key})
        OperationalChangeAudit.record(
            actor=requested_by,
            target_type=f"{source_type}_source",
            target_id=canonical_key,
            action="crawl_failed",
            new_value={"run_id": run.pk, "outcome": outcome},
            confirmed=True,
        )
        return run


__all__ = ["CrawlRequestError", "resolve_source", "trigger_crawl"]
