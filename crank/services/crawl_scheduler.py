# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Bounded freshness planning for company-profile crawls.

Since issue #462 job-source ingestion has a single owner: the job pipeline
(``run_job_pipeline``). The scheduler plans organization-profile crawls only;
``--phase jobs`` is an accepted, documented no-op.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from django.conf import settings
from django.utils import timezone

from crank.models.job import JobSourceCatalog
from crank.models.source import ApprovalState, SourceCatalog
from crank.services import monitoring, source_freshness
from crank.services.company_crawler import crawl_company_profile


PHASE_ORGANIZATIONS = "organization"
# Accepted for operator-script compatibility; a documented no-op since #462:
# job-source ingestion is owned by run_job_pipeline.
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


def _dispatch_organization(source: SourceCatalog, now: datetime) -> Any:
    return crawl_company_profile(source, now=now)


def plan_crawls(
    *,
    phase: str = PHASE_ALL,
    now: datetime | None = None,
    max_sources: int | None = None,
    deadline_seconds: int | None = None,
    dispatchers: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, int | bool]:
    """Dispatch stale approved organization sources within explicit budgets.

    Source names and payloads never enter telemetry. Due sources are taken
    least recently attempted first (pk breaks ties), so a failing source
    cannot starve the others. ``last_crawl_at`` advances only on a fully
    successful dispatch; every attempt records ``last_attempt_at`` and the
    failure streak that drives retry backoff. Due sources the budget or
    deadline did not reach are left untouched (``deferred_budget``).

    ``phase=PHASE_JOBS`` is a documented no-op since #462 (the job pipeline is
    the single job-ingestion owner): it returns bounded counts only — each
    present job source is reported as skipped with no dispatch — while
    ``PHASE_ORGANIZATIONS`` and ``PHASE_ALL`` plan organization profiles.
    """
    if phase not in PHASES:
        raise ValueError(f"unsupported crawl phase: {phase}")
    reference = now or timezone.now()
    counts: dict[str, int | bool] = {
        "scheduled": 0,
        "stale": 0,
        "skipped": 0,
        "errors": 0,
        "organizations_total": 0,
        "jobs_total": 0,
        "eligible": 0,
        "skipped_policy": 0,
        "skipped_fresh": 0,
        "deferred_backoff": 0,
        "deferred_budget": 0,
        "succeeded": 0,
        "partial": 0,
        "failed": 0,
        "oldest_due_age_hours": 0,
    }
    if phase == PHASE_JOBS:
        counts["jobs_total"] = JobSourceCatalog.objects.count()
        counts["skipped"] = int(counts["jobs_total"])
        monitoring.record_event("crawl_planning", counts)
        return counts
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

    organization_sources = list(SourceCatalog.objects.all().order_by("pk"))
    counts["organizations_total"] = len(organization_sources)
    selection = source_freshness.select_due(
        organization_sources,
        source_freshness.organization_policy(),
        now=reference,
        approved_value=ApprovalState.APPROVED,
        limit=limit,
    )
    counts.update(
        {
            key: selection.counts[key]
            for key in (
                "eligible",
                "skipped_policy",
                "skipped_fresh",
                "deferred_backoff",
            )
        }
    )
    counts["stale"] = selection.counts["due"]
    counts["oldest_due_age_hours"] = selection.oldest_due_age_hours or 0

    outcome_keys = {
        source_freshness.Outcome.SUCCESS: "succeeded",
        source_freshness.Outcome.PARTIAL: "partial",
        source_freshness.Outcome.FAILED: "failed",
    }
    for source in selection.selected:
        if time.monotonic() >= deadline:
            break
        dispatcher = dispatchers.get(PHASE_ORGANIZATIONS, _dispatch_organization)
        try:
            result = dispatcher(source, reference)
            result_errors = _result_errors(result)
            outcome = source_freshness.organization_outcome(result)
        except Exception:
            result_errors = 1
            outcome = source_freshness.Outcome.FAILED
        counts["scheduled"] += 1
        counts["errors"] += result_errors
        counts[outcome_keys[outcome]] += 1
        source_freshness.record_outcome(
            SourceCatalog, source.pk, outcome, now=reference
        )

    counts["deferred_budget"] = counts["stale"] - counts["scheduled"]
    counts["skipped"] = counts["organizations_total"] - counts["scheduled"]

    monitoring.record_event("crawl_planning", counts)
    return counts
