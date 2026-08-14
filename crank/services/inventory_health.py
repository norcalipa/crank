# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Read-only inventory health checks for the production job-source bootstrap.

These helpers compute bounded, low-cardinality signals operators use to detect
a broken or stalled job inventory: zero enabled sources, zero active listings,
stale sources, repeated crawl failures, and listing-count collapse. They never
construct providers, touch credentials, or make network calls, so they are safe
to run in CI, from a CronJob, or from the Django shell.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.utils import timezone

from crank.agents.jobs.registry import REGISTRY
from crank.models.crawl_run import CrawlRun
from crank.models.job import JobListing, JobSourceCatalog

#: Crawl outcomes that count as a failed crawl for repeated-failure detection.
FAILURE_OUTCOMES = frozenset({CrawlRun.Outcome.FAILURE, CrawlRun.Outcome.TIMEOUT})

#: How many consecutive failed crawls mark a source as repeatedly failing.
DEFAULT_MIN_CONSECUTIVE_FAILURES = 3


def _setting_int(name: str, default: int) -> int:
    value = getattr(settings, name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def freshness_hours() -> int:
    return max(0, _setting_int("JOB_FRESHNESS_HOURS", 24))


def _is_stale(last_crawl_at: datetime | None, reference: datetime) -> bool:
    if last_crawl_at is None:
        return True
    hours = freshness_hours()
    if hours <= 0:
        # A non-positive freshness target disables staleness reporting.
        return False
    return reference - last_crawl_at >= timedelta(hours=hours)


def adapter_registered(adapter_key: str) -> bool:
    """Return whether an adapter key exists in the code-owned registry."""
    return REGISTRY.get(adapter_key or "") is not None


def check_inventory_health(*, now: datetime | None = None) -> dict[str, Any]:
    """Compute inventory health signals and human-readable violations.

    The result is a flat mapping of scalars plus a ``violations`` list and a
    ``healthy`` boolean. Only scalar, allowlisted values are intended for
    telemetry; the ``violations`` list is for operator output and never emitted.
    """
    reference = now or timezone.now()
    sources_total = JobSourceCatalog.objects.count()
    # Single bounded query set: per-source active-listing counts and the
    # "ever produced listings" signal are resolved in SQL, and crawl runs are
    # prefetched once (ordered) so the per-source loop issues no additional
    # queries and only inspects the first min_failures outcomes.
    approved_enabled = list(
        JobSourceCatalog.objects.filter(
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        .order_by("pk")
        .annotate(
            source_active=Count(
                "listings", filter=Q(listings__status=JobListing.Status.ACTIVE)
            ),
            produced_listings=Exists(
                CrawlRun.objects.filter(
                    job_source=OuterRef("pk"),
                    outcome__in=[
                        CrawlRun.Outcome.SUCCESS,
                        CrawlRun.Outcome.PARTIAL,
                    ],
                    counts__listings_ingested__gt=0,
                )
            ),
        )
        .prefetch_related(
            Prefetch(
                "crawl_runs",
                queryset=CrawlRun.objects.order_by("-started_at", "-id"),
            )
        )
    )
    active_listings = JobListing.objects.count()
    min_failures = max(
        1,
        _setting_int("CRAWL_REPEATED_FAILURE_THRESHOLD", DEFAULT_MIN_CONSECUTIVE_FAILURES),
    )

    stale_sources = 0
    repeated_failure_sources = 0
    collapsed_sources = 0
    unregistered_adapter_sources = 0

    for source in approved_enabled:
        if _is_stale(source.last_crawl_at, reference):
            stale_sources += 1

        if not adapter_registered(source.adapter_key):
            unregistered_adapter_sources += 1

        runs = source.crawl_runs.all()
        recent_outcomes = [run.outcome for run in runs[:min_failures]]
        if len(recent_outcomes) >= min_failures and all(
            outcome in FAILURE_OUTCOMES for outcome in recent_outcomes
        ):
            repeated_failure_sources += 1

        if source.source_active == 0 and source.produced_listings:
            collapsed_sources += 1

    zero_enabled_sources = len(approved_enabled) == 0
    zero_active_listings = len(approved_enabled) > 0 and active_listings == 0

    violations: list[str] = []
    if zero_enabled_sources:
        violations.append("no approved and enabled job sources")
    if zero_active_listings:
        violations.append("zero active listings")
    if stale_sources:
        violations.append(f"{stale_sources} enabled source(s) are stale")
    if repeated_failure_sources:
        violations.append(
            f"{repeated_failure_sources} source(s) with repeated crawl failures"
        )
    if collapsed_sources:
        violations.append(f"{collapsed_sources} source(s) collapsed to zero active listings")
    if unregistered_adapter_sources:
        violations.append(
            f"{unregistered_adapter_sources} enabled source(s) with an unregistered adapter"
        )

    return {
        "sources_total": sources_total,
        "enabled_sources": len(approved_enabled),
        "active_listings": active_listings,
        "stale_sources": stale_sources,
        "repeated_failure_sources": repeated_failure_sources,
        "collapsed_sources": collapsed_sources,
        "unregistered_adapter_sources": unregistered_adapter_sources,
        "violations": violations,
        "healthy": not violations,
    }


__all__ = [
    "DEFAULT_MIN_CONSECUTIVE_FAILURES",
    "FAILURE_OUTCOMES",
    "adapter_registered",
    "check_inventory_health",
    "freshness_hours",
]
