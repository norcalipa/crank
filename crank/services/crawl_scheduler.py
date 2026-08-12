# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Bounded freshness planning for company and job-source crawls."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from django.conf import settings
from django.utils import timezone

from crank.agents.jobs.base import JobSourceQuery
from crank.agents.jobs.ingest import ingest_jobs
from crank.models.job import JobSourceCatalog
from crank.models.source import ApprovalState, SourceCatalog
from crank.services import monitoring
from crank.services.company_crawler import crawl_company_profile


PHASE_ORGANIZATIONS = "organization"
PHASE_JOBS = "jobs"
PHASE_ALL = "all"
PHASES = frozenset({PHASE_ORGANIZATIONS, PHASE_JOBS, PHASE_ALL})


def is_stale(
    last_crawl_at: datetime | None,
    freshness_hours: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a source has no crawl or has exceeded its freshness TTL."""
    if last_crawl_at is None:
        return True
    reference = now or timezone.now()
    return reference - last_crawl_at >= timedelta(hours=max(0, freshness_hours))


def _setting(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def _result_errors(result: Any) -> int:
    try:
        return max(0, int(getattr(result, "errors", 0)))
    except (TypeError, ValueError):
        return 1


def _mark_crawled(source: Any, crawled_at: datetime) -> None:
    source.last_crawl_at = crawled_at
    source.save(update_fields=["last_crawl_at", "modified"])


def _dispatch_organization(source: SourceCatalog, now: datetime) -> Any:
    return crawl_company_profile(source, now=now)


def _dispatch_job(
    source: JobSourceCatalog, now: datetime, max_listings: int, max_pages: int
) -> Any:
    del now  # The job ingestion contract timestamps listings from the adapter.
    return ingest_jobs(
        source,
        JobSourceQuery(
            max_listings=max(1, min(max_listings, 10000)),
            max_pages=max(1, min(max_pages, 1000)),
        ),
    )


def plan_crawls(
    *,
    phase: str = PHASE_ALL,
    now: datetime | None = None,
    max_sources: int | None = None,
    deadline_seconds: int | None = None,
    dispatchers: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, int | bool]:
    """Dispatch stale approved sources within explicit source and time budgets.

    Source names and payloads never enter telemetry. A source's timestamp is
    advanced only after a dispatch completes without provider errors, keeping
    failed sources stale for the next bounded retry while preserving freshness
    guarantees.
    """
    if phase not in PHASES:
        raise ValueError(f"unsupported crawl phase: {phase}")
    reference = now or timezone.now()
    limit = max(
        0,
        int(
            max_sources
            if max_sources is not None
            else _setting("CRAWL_MAX_SOURCES", 10)
        ),
    )
    deadline = time.monotonic() + max(
        0.0,
        float(
            deadline_seconds
            if deadline_seconds is not None
            else _setting("CRAWL_DEADLINE_SECONDS", 300)
        ),
    )
    dispatchers = dispatchers or {}
    counts: dict[str, int | bool] = {
        "scheduled": 0,
        "stale": 0,
        "skipped": 0,
        "errors": 0,
        "organizations_total": 0,
        "jobs_total": 0,
    }

    sources: list[tuple[str, Any, int]] = []
    if phase in {PHASE_ALL, PHASE_ORGANIZATIONS}:
        organization_sources = list(SourceCatalog.objects.all().order_by("pk"))
        counts["organizations_total"] = len(organization_sources)
        sources.extend(
            (
                PHASE_ORGANIZATIONS,
                source,
                int(_setting("ORGANIZATION_FRESHNESS_HOURS", 168)),
            )
            for source in organization_sources
        )
    if phase in {PHASE_ALL, PHASE_JOBS}:
        job_sources = list(JobSourceCatalog.objects.all().order_by("pk"))
        counts["jobs_total"] = len(job_sources)
        sources.extend(
            (PHASE_JOBS, source, int(_setting("JOB_FRESHNESS_HOURS", 24)))
            for source in job_sources
        )

    for source_phase, source, freshness_hours in sources:
        approved_state = (
            ApprovalState.APPROVED
            if source_phase == PHASE_ORGANIZATIONS
            else JobSourceCatalog.ApprovalState.APPROVED
        )
        approved = source.approval_state == approved_state
        if not approved or not source.enabled:
            counts["skipped"] += 1
            continue
        if not is_stale(source.last_crawl_at, freshness_hours, now=reference):
            counts["skipped"] += 1
            continue
        counts["stale"] += 1
        if counts["scheduled"] >= limit or time.monotonic() >= deadline:
            counts["skipped"] += 1
            continue
        try:
            if source_phase == PHASE_ORGANIZATIONS:
                dispatcher = dispatchers.get(PHASE_ORGANIZATIONS, _dispatch_organization)
                result = dispatcher(source, reference)
            else:
                dispatcher = dispatchers.get(PHASE_JOBS, _dispatch_job)
                result = dispatcher(
                    source,
                    reference,
                    int(_setting("CRAWL_MAX_JOB_LISTINGS", 100)),
                    int(_setting("CRAWL_MAX_PAGES", 10)),
                )
            counts["scheduled"] += 1
            result_errors = _result_errors(result)
            counts["errors"] += result_errors
            if result_errors == 0:
                _mark_crawled(source, reference)
        except Exception:
            counts["scheduled"] += 1
            counts["errors"] += 1

    monitoring.record_event("crawl_planning", counts)
    return counts
