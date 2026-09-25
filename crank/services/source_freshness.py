# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Fair, bounded and truthful freshness scheduling shared by both planners.

Three notions are kept apart on purpose:

* **dispatch frequency** - how often a CronJob starts a planner;
* **refresh TTL** - how long a *successful* fetch keeps a source out of the
  next plan (``RefreshPolicy.ttl``);
* **published evidence age** - field-level ``last_verified_at`` and a source's
  ``last_crawl_at`` (last *successful* fetch), never advanced by failures.

``last_attempt_at`` and ``consecutive_failures`` are scheduling state only:
they order dispatch and drive retry backoff, and are never shown as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable

from django.conf import settings
from django.db.models import F

MAX_AGE_HOURS = 8760  # telemetry cap: one year

CLASS_POLICY = "policy"
CLASS_FRESH = "fresh"
CLASS_BACKOFF = "backoff"
CLASS_DUE = "due"


class Outcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class RefreshPolicy:
    ttl: timedelta
    tolerance: timedelta
    retry_base: timedelta
    retry_max: timedelta


@dataclass
class Selection:
    selected: list = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    oldest_due_age_hours: int | None = None


def _hours(name: str, default: int) -> int:
    return max(0, int(getattr(settings, name, default)))


def _policy(ttl_hours: int) -> RefreshPolicy:
    return RefreshPolicy(
        ttl=timedelta(hours=ttl_hours),
        tolerance=timedelta(minutes=_hours("CRAWL_SCHEDULE_TOLERANCE_MINUTES", 15)),
        retry_base=timedelta(minutes=_hours("SOURCE_RETRY_BACKOFF_MINUTES", 60)),
        retry_max=timedelta(hours=_hours("SOURCE_RETRY_BACKOFF_MAX_HOURS", 24)),
    )


def organization_policy() -> RefreshPolicy:
    return _policy(_hours("ORGANIZATION_FRESHNESS_HOURS", 168))


def job_policy() -> RefreshPolicy:
    return _policy(_hours("JOB_REFRESH_TTL_HOURS", 6))


def retry_after(failures: int, policy: RefreshPolicy) -> timedelta:
    """Exponential backoff after *failures* consecutive failed attempts."""
    if failures <= 0:
        return timedelta(0)
    # Cap the exponent so a huge counter cannot build an enormous timedelta.
    return min(policy.retry_base * (2 ** min(failures - 1, 20)), policy.retry_max)


def next_eligible_at(source: Any, policy: RefreshPolicy) -> datetime | None:
    """When *source* is next due, or ``None`` when it is due now."""
    candidates = []
    if source.last_crawl_at is not None:
        candidates.append(source.last_crawl_at + policy.ttl - policy.tolerance)
    if source.consecutive_failures and source.last_attempt_at is not None:
        candidates.append(
            source.last_attempt_at
            + retry_after(source.consecutive_failures, policy)
        )
    return max(candidates) if candidates else None


def classify(source: Any, policy: RefreshPolicy, *, now: datetime, approved_value: str) -> str:
    if source.approval_state != approved_value or not source.enabled:
        return CLASS_POLICY
    if (
        source.last_crawl_at is not None
        and now - source.last_crawl_at < policy.ttl - policy.tolerance
    ):
        return CLASS_FRESH
    if (
        source.consecutive_failures
        and source.last_attempt_at is not None
        and now < source.last_attempt_at + retry_after(source.consecutive_failures, policy)
    ):
        return CLASS_BACKOFF
    return CLASS_DUE


def _priority(source: Any) -> tuple:
    attempt, crawl = source.last_attempt_at, source.last_crawl_at
    return (
        attempt is not None,
        attempt or datetime.min,
        crawl is not None,
        crawl or datetime.min,
        source.pk,
    )


def _age_hours(source: Any, now: datetime) -> int:
    if source.last_crawl_at is None:
        return MAX_AGE_HOURS
    return max(0, min(MAX_AGE_HOURS, int((now - source.last_crawl_at).total_seconds() // 3600)))


def select_due(
    sources: Iterable[Any],
    policy: RefreshPolicy,
    *,
    now: datetime,
    approved_value: str,
    limit: int,
) -> Selection:
    """Pick up to *limit* due sources, least recently attempted first."""
    counts = {
        "eligible": 0,
        "skipped_policy": 0,
        "skipped_fresh": 0,
        "deferred_backoff": 0,
        "due": 0,
    }
    due = []
    for source in sources:
        state = classify(source, policy, now=now, approved_value=approved_value)
        if state == CLASS_POLICY:
            counts["skipped_policy"] += 1
            continue
        counts["eligible"] += 1
        if state == CLASS_FRESH:
            counts["skipped_fresh"] += 1
        elif state == CLASS_BACKOFF:
            counts["deferred_backoff"] += 1
        else:
            counts["due"] += 1
            due.append(source)
    due.sort(key=_priority)
    oldest = max((_age_hours(s, now) for s in due), default=None)
    return Selection(selected=due[: max(0, limit)], counts=counts, oldest_due_age_hours=oldest)


def organization_outcome(result: Any) -> Outcome:
    try:
        errors = max(0, int(getattr(result, "errors", 0)))
    except (TypeError, ValueError):
        errors = 1
    return Outcome.SUCCESS if errors == 0 else Outcome.PARTIAL


def job_outcome(result: Any) -> Outcome:
    if not int(getattr(result, "errors", 0) or 0):
        return Outcome.SUCCESS
    if getattr(result, "closure_skipped_reason", "") == "fetch_error":
        return Outcome.FAILED
    return Outcome.PARTIAL


def record_outcome(model: Any, pk: Any, outcome: Outcome, *, now: datetime) -> None:
    """Persist one attempt; only SUCCESS moves ``last_crawl_at``.

    A queryset ``update`` keeps concurrent admin edits (``enabled``,
    ``approval_state``) intact and never runs ``save()`` side effects.
    """
    queryset = model.objects.filter(pk=pk)
    if outcome == Outcome.SUCCESS:
        queryset.update(last_crawl_at=now, last_attempt_at=now, consecutive_failures=0)
    else:
        queryset.update(
            last_attempt_at=now,
            consecutive_failures=F("consecutive_failures") + 1,
        )


def outcome_from_crawl_run(run_outcome: str) -> Outcome:
    if run_outcome == "success":
        return Outcome.SUCCESS
    if run_outcome == "partial":
        return Outcome.PARTIAL
    return Outcome.FAILED
