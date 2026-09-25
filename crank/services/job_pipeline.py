# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Bounded orchestration for periodic job ingestion and user matching."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import time
from typing import Any, Mapping

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from crank.agents.jobs.base import JobSourceQuery
from crank.agents.jobs.employer import resolve_employer
from crank.agents.jobs.ingest import JobIngestResult
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG
from crank.models.agent_run import AgentRun
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.preference import UserPreference, default_preferences
from crank.services import agent_runs, match_recompute
from crank.services import source_freshness
from crank.services.job_ingest import ingest_job_source, record_source_publication

logger = logging.getLogger(__name__)

COUNT_KEYS = (
    "sources_total",
    "sources_succeeded",
    "sources_failed",
    "sources_skipped",
    "sources_eligible",
    "sources_deferred",
    "oldest_source_age_hours",
    "listings_ingested",
    "listings_updated",
    "listings_closed",
    "listings_expired",
    "listings_deleted",
    "employers_resolved",
    "employers_unresolved",
    "users_total",
    "users_succeeded",
    "users_failed",
    "matches_persisted",
    "stale_discarded",
    "duplicate_skipped",
    "deadline_reached",
)

# Retention sweep defaults (issue #469), overridable per source through the
# reserved ``catalog_metadata`` keys documented in
# ``docs/job-source-catalog.md``. ``deletion_days`` must exceed
# ``expiry_days``; a misconfiguration falls back to these defaults rather
# than deleting early. Each phase is bounded per run so a large backlog
# cannot blow the pipeline deadline, and the sweep is resumable: the next
# run continues where the bounded run stopped.
DEFAULT_EXPIRY_DAYS = 30
DEFAULT_DELETION_DAYS = 90
RETENTION_SWEEP_LIMIT = 500


class JobPipelineError(RuntimeError):
    """Raised when a non-empty pipeline population completely fails."""

    def __init__(self, message: str, counts: dict[str, int | bool]):
        super().__init__(message)
        self.counts = counts


@dataclass(frozen=True)
class _Deadline:
    until: float

    def reached(self) -> bool:
        return time.monotonic() >= self.until


def _empty_counts() -> dict[str, int | bool]:
    return {key: False if key == "deadline_reached" else 0 for key in COUNT_KEYS}


def _setting(options: Mapping[str, Any], name: str, default: Any) -> Any:
    option_name = name.lower()
    short_name = option_name.removeprefix("job_pipeline_")
    if name in options:
        return options[name]
    if option_name in options:
        return options[option_name]
    if short_name in options:
        return options[short_name]
    return getattr(settings, name, default)


def _source_query(options: Mapping[str, Any], max_listings: int) -> JobSourceQuery:
    query = options.get("query")
    if isinstance(query, JobSourceQuery):
        return query
    values = dict(query) if isinstance(query, Mapping) else {}
    values.setdefault("max_listings", max(1, min(int(max_listings), 10000)))
    values.setdefault("max_pages", int(options.get("max_pages", 10)))
    return JobSourceQuery(**values)


def _adapter_for(source: Any, options: Mapping[str, Any]) -> Any:
    adapter = options.get("adapter")
    if isinstance(adapter, Mapping):
        return adapter.get(source.pk)
    if callable(adapter) and not hasattr(adapter, "fetch"):
        return adapter(source)
    return adapter


def _is_meaningful(value: Any) -> bool:
    """Return whether a preference document contains an active choice."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_is_meaningful(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_is_meaningful(item) for item in value)
    return True


def _has_active_preferences(value: Any, baseline: Any = None) -> bool:
    """Return whether a preference document changes an active choice."""
    if baseline is None:
        baseline = default_preferences()
    if isinstance(value, Mapping):
        baseline = baseline if isinstance(baseline, Mapping) else {}
        return any(
            _has_active_preferences(item, baseline.get(key))
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_is_meaningful(item) for item in value)
    return value != baseline and _is_meaningful(value)


def _eligible_preferences():
    """Select opted-in users with at least one non-empty preference value."""
    rows = UserPreference.objects.filter(user__is_active=True).select_related(
        "user"
    ).order_by("user_id")
    return [row for row in rows if _has_active_preferences(row.preferences)]


def _resolve_source_listings(source: Any, before_ids: set[int]) -> tuple[int, int]:
    """Resolve listings touched by ingestion, isolating each lookup failure."""
    resolved = unresolved = 0
    listings = JobListing.all_objects.filter(source=source).order_by("pk")
    for listing in listings:
        # Existing listings can be returned by an update/replay; resolving them
        # again is intentional because operator aliases may have changed.
        if listing.pk not in before_ids or listing.status == JobListing.Status.ACTIVE:
            try:
                result = resolve_employer(listing)
                if getattr(result, "resolved", False):
                    resolved += 1
                else:
                    unresolved += 1
            except Exception as exc:  # noqa: BLE001 - one listing must not stop a source
                unresolved += 1
                logger.warning(
                    "job employer resolution failed: source_id=%s error=%s",
                    source.pk,
                    agent_runs.sanitize_error(exc),
                )
    return resolved, unresolved


def _retention_days(source: Any) -> tuple[int, int]:
    """Per-source retention windows with a safe fallback (issue #469)."""
    metadata = getattr(source, "catalog_metadata", None) or {}
    expiry = metadata.get("expiry_days", DEFAULT_EXPIRY_DAYS)
    deletion = metadata.get("deletion_days", DEFAULT_DELETION_DAYS)
    try:
        expiry = int(expiry)
        deletion = int(deletion)
    except (TypeError, ValueError):
        return DEFAULT_EXPIRY_DAYS, DEFAULT_DELETION_DAYS
    if expiry < 1 or deletion <= expiry:
        return DEFAULT_EXPIRY_DAYS, DEFAULT_DELETION_DAYS
    return expiry, deletion


def _proven_complete_snapshot(result: JobIngestResult) -> bool:
    """Whether a fetch constitutes a proven, complete inventory snapshot.

    Retention and absence-based closure are both gated on this (issue #469
    review): a failed, incomplete, truncated, filtered, or operator-disabled
    fetch must never expire or delete rows, or it would read a partial page as
    "everything absent" and destroy accepted inventory and match history.

    ``closure_skipped_reason`` is the single authoritative "may mutate the
    whole source" signal produced by ``ingest_jobs``' ``_closure_decision``:
    an empty reason means closure was allowed (complete, untruncated,
    unfiltered, and not disabled by the catalog kill switch). Retention must
    not reconstruct a weaker proof that drops those two conditions.
    """
    return bool(
        getattr(result, "complete_snapshot", False)
        and not getattr(result, "truncated", False)
        and not getattr(result, "errors", 0)
        and not getattr(result, "closure_skipped_reason", "")
    )


def _pre_ingest_deletion_candidates(
    source: Any, *, limit: int = RETENTION_SWEEP_LIMIT
) -> set[int]:
    """Bounded snapshot of rows already terminal and past the deletion window.

    Taken *before* ingestion runs absence closure (issue #469 review). The
    deletion phase deletes only ids in this set, so a row that entered the
    source run active (and is absence-closed by ingestion, or expired by the
    sweep itself) is never deleted in the same run — without materializing the
    source's entire active inventory. Bounded to ``limit`` so a large source
    never serializes O(all rows) into the terminal query.
    """
    _, deletion_days = _retention_days(source)
    cutoff = timezone.now() - timedelta(days=deletion_days)
    return set(
        JobListing.all_objects.filter(
            source=source,
            status__in=[JobListing.Status.CLOSED, JobListing.Status.EXPIRED],
            last_seen_at__lt=cutoff,
        )
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )


def _retention_sweep(
    source: Any,
    *,
    limit: int = RETENTION_SWEEP_LIMIT,
    deletion_candidates: set[int] | None = None,
) -> tuple[int, int]:
    """Expire stale active listings and delete aged terminal ones (issue #469).

    Bounded per run and resumable. Expiry uses the source's retention window
    on ``last_seen_at``; deletion applies only to terminal listings past the
    longer deletion window that were already candidate-before this run's
    ingestion, via ``deletion_candidates`` (the bounded pre-ingest snapshot;
    recomputed on the spot when the caller has not run ingestion first). An
    ``active`` listing is never deleted, and a row that was active when the
    run began is expired, never deleted, in that same run.
    """
    if deletion_candidates is None:
        deletion_candidates = _pre_ingest_deletion_candidates(source, limit=limit)
    expiry_days, deletion_days = _retention_days(source)
    now = timezone.now()
    expired = JobListing.all_objects.filter(
        source=source,
        status=JobListing.Status.ACTIVE,
        last_seen_at__lt=now - timedelta(days=expiry_days),
    ).order_by("pk")[:limit]
    expired_ids = list(expired.values_list("pk", flat=True))
    expired_count = 0
    if expired_ids:
        expired_count = JobListing.all_objects.filter(pk__in=expired_ids).update(
            status=JobListing.Status.EXPIRED
        )
    terminal = JobListing.all_objects.filter(
        source=source,
        pk__in=deletion_candidates,
        status__in=[JobListing.Status.CLOSED, JobListing.Status.EXPIRED],
        last_seen_at__lt=now - timedelta(days=deletion_days),
    ).order_by("pk")[:limit]
    deleted_count = 0
    for listing in terminal:
        # Row-by-row delete so JobMatch/UnresolvedEmployer cascades fire.
        listing.delete()
        deleted_count += 1
    return expired_count, deleted_count


def _ingest_source(
    source: Any, options: Mapping[str, Any], before_ids: set[int]
) -> tuple[JobIngestResult | None, int, int, bool]:
    """Ingest through the shared single-owner boundary (issue #462).

    Returns ``(result, resolved, unresolved, skipped)``; ``result`` is ``None``
    exactly when ``skipped`` is ``True`` (the per-source lock was held by
    another ingestion path, which records its own sanitized skip event).
    """
    query = _source_query(
        options,
        _setting(options, "JOB_PIPELINE_MAX_LISTINGS_PER_USER", 500),
    )
    ingestion = ingest_job_source(
        source, query=query, adapter=_adapter_for(source, options)
    )
    if ingestion.skipped:
        return None, 0, 0, True
    result = ingestion.result
    resolved, unresolved = _resolve_source_listings(source, before_ids)
    # Attach resolution totals for the caller without changing the public
    # ingest result dataclass or its adapter contract.
    return result, resolved, unresolved, False


def _run_user(
    user_preference: UserPreference,
    options: Mapping[str, Any],
) -> match_recompute.RecomputeOutcome:
    """Recompute and CAS-publish one user's committed generation (issue #475).

    Delegates to :func:`crank.services.match_recompute.recompute_user`, which
    owns its own bounded inventory snapshot
    (:func:`crank.agents.jobs.match_persist.match_inventory`) — the shared
    pre-read that used to live here (``_active_listings``) is gone; each
    user's snapshot is now taken under that user's own state-row lock. Not
    gated by the ``match_recompute``/``match_results_read`` switches: this
    fixes the existing, already-enabled ``job_pipeline`` capability.

    Passes this run's resolved ``JOB_PIPELINE_MAX_LISTINGS_PER_USER``
    through to ``recompute_user`` (issue #475 review round 2, MINOR finding
    6): ``_ingest_source`` already honors a per-run ``options`` override for
    this same setting via :func:`_setting`, so recompute must use the same
    resolved value — otherwise a bounded operator or test run ingests one
    listing window but ranks against the unbounded global default.
    """
    config = options.get("ranking_config") or DEFAULT_CONFIG
    max_listings = _setting(options, "JOB_PIPELINE_MAX_LISTINGS_PER_USER", 500)
    return match_recompute.recompute_user(
        user_preference.user,
        reason="pipeline",
        config=config,
        max_listings=max_listings,
    )


def run_job_pipeline(run: AgentRun, **options) -> dict[str, int | bool]:
    """Ingest approved sources and persist bounded, idempotent user matches."""
    counts = _empty_counts()
    deadline = _Deadline(
        time.monotonic()
        + max(
            0.0,
            float(_setting(options, "JOB_PIPELINE_DEADLINE_SECONDS", 300)),
        )
    )
    max_sources = max(0, int(_setting(options, "JOB_PIPELINE_MAX_SOURCES", 10)))
    max_users = max(0, int(_setting(options, "JOB_PIPELINE_MAX_USERS", 100)))

    reference = options.get("now") or timezone.now()
    selection = source_freshness.select_due(
        JobSourceCatalog.objects.all().order_by("pk"),
        source_freshness.job_policy(),
        now=reference,
        approved_value=JobSourceCatalog.ApprovalState.APPROVED,
        limit=max_sources,
    )
    counts["sources_eligible"] = selection.counts["eligible"]
    # The budget counts real attempts: a lock-skipped source records no
    # attempt, so it must not consume a slot and starve later due sources.
    attempts = 0
    visited = 0
    counts["oldest_source_age_hours"] = selection.oldest_due_age_hours or 0
    successful_sources = 0
    for source in selection.ordered:
        if attempts >= max_sources:
            break
        if deadline.reached():
            counts["deadline_reached"] = True
            break
        visited += 1
        before_ids = set(
            JobListing.all_objects.filter(source=source).values_list("pk", flat=True)
        )
        # Bounded pre-ingest snapshot of terminal rows already past the
        # deletion window (issue #469 review): the deletion phase deletes
        # only these ids, so a row active at the start of the run that is
        # absence-closed by ingestion (and is older than the deletion window)
        # is expired, never deleted, in this same run — without materializing
        # the source's entire active inventory.
        deletion_candidates = _pre_ingest_deletion_candidates(source)
        # Snapshot the source's pre-stage organization ids too: a listing
        # reassigned or unresolved during this stage makes its *former*
        # organization affected, and only the union of the before/after ids
        # covers every organization whose caches need invalidation.
        before_org_ids = set(
            JobListing.all_objects.filter(
                source=source, organization__isnull=False
            )
            .order_by()
            .values_list("organization_id", flat=True)
            .distinct()
        )
        try:
            # One transaction per source stage: the accepted writes and their
            # publication events commit together, so an outbox insert failure
            # (or any crash inside the block) rolls the writes back and
            # committed data can never be left without its event. A source
            # with partial row failures still publishes the rows it accepted.
            with transaction.atomic():
                result, resolved, unresolved, skipped = _ingest_source(
                    source, options, before_ids
                )
                if skipped:
                    counts["sources_skipped"] += 1
                    continue
                attempts += 1
                if _proven_complete_snapshot(result):
                    expired_count, deleted_count = _retention_sweep(
                        source, deletion_candidates=deletion_candidates
                    )
                else:
                    # A failed, incomplete, truncated, filtered, or disabled
                    # fetch must never expire or delete rows (issue #469
                    # review).
                    expired_count = deleted_count = 0
                if (
                    int(result.ingested)
                    or int(result.updated)
                    or resolved
                    or unresolved
                    or int(result.closed)
                    or int(result.expired)
                    or expired_count
                    or deleted_count
                ):
                    record_source_publication(
                        source, result, resolved, unresolved, before_org_ids
                    )
                source_freshness.record_outcome(
                    JobSourceCatalog,
                    source.pk,
                    source_freshness.job_outcome(result),
                    now=reference,
                )
            counts["listings_ingested"] += int(result.ingested)
            counts["listings_updated"] += int(result.updated)
            counts["listings_closed"] += int(result.closed)
            counts["listings_expired"] += int(result.expired) + expired_count
            counts["listings_deleted"] += deleted_count
            counts["employers_resolved"] += resolved
            counts["employers_unresolved"] += unresolved
            if int(result.errors):
                counts["sources_failed"] += 1
                agent_runs.monitoring.record_event(
                    "source_stage",
                    {
                        "stage": "job_ingest",
                        "source_key": source.adapter_key,
                        "status": "failed",
                        "reason_code": "rejected",
                    },
                )
            else:
                counts["sources_succeeded"] += 1
                successful_sources += 1
                agent_runs.monitoring.record_event(
                    "source_stage",
                    {
                        "stage": "job_ingest",
                        "source_key": source.adapter_key,
                        "status": "succeeded",
                        "items_succeeded": int(result.ingested) + int(result.updated),
                        "listings_closed": int(result.closed),
                        "listings_expired": int(result.expired) + expired_count,
                        "listings_deleted": deleted_count,
                        "reason_code": result.closure_skipped_reason or "none",
                    },
                )
        except Exception as exc:  # noqa: BLE001 - isolate source failures
            attempts += 1
            counts["sources_failed"] += 1
            # The stage transaction rolled back; record the failed attempt
            # outside it so backoff and fairness still see it.
            source_freshness.record_outcome(
                JobSourceCatalog,
                source.pk,
                source_freshness.Outcome.FAILED,
                now=reference,
            )
            agent_runs.monitoring.record_event(
                "source_stage",
                {
                    "stage": "job_ingest",
                    "source_key": source.adapter_key,
                    "status": "failed",
                    "reason_code": agent_runs.monitoring.failure_reason(exc),
                },
            )
            logger.warning(
                "job source failed: source_id=%s error=%s",
                source.pk,
                agent_runs.sanitize_error(exc),
            )

    counts["sources_total"] = visited
    # Everything due but not visited: over budget or past the deadline.
    counts["sources_deferred"] = max(0, selection.counts["due"] - visited)

    preferences = _eligible_preferences()[:max_users]
    counts["users_total"] = len(preferences)
    successful_users = 0
    if deadline.reached():
        counts["deadline_reached"] = True
    for preference in preferences:
        if deadline.reached():
            counts["deadline_reached"] = True
            break
        try:
            outcome = _run_user(preference, options)
            status = outcome.status
            if status == match_recompute.RecomputeStatus.PUBLISHED:
                counts["matches_persisted"] += outcome.persisted
                counts["users_succeeded"] += 1
                successful_users += 1
            elif status == match_recompute.RecomputeStatus.CURRENT:
                counts["duplicate_skipped"] += 1
                counts["users_succeeded"] += 1
                successful_users += 1
            elif status == match_recompute.RecomputeStatus.DISCARDED_STALE:
                counts["stale_discarded"] += 1
                counts["users_succeeded"] += 1
                successful_users += 1
            elif status in (
                match_recompute.RecomputeStatus.NO_PREFERENCES,
                match_recompute.RecomputeStatus.USER_GONE,
            ):
                counts["users_succeeded"] += 1
                successful_users += 1
            else:
                counts["users_failed"] += 1
        except Exception as exc:  # noqa: BLE001 - isolate user failures
            counts["users_failed"] += 1
            logger.warning(
                "job user matching failed: user_id=%s error=%s",
                preference.user_id,
                agent_runs.sanitize_error(exc),
            )

    agent_runs.record_agent_event(
        run,
        "job_pipeline_completed",
        counts=counts,
        completed_at=timezone.now().isoformat(),
    )
    agent_runs.monitoring.record_event(
        "matching_batch",
        {
            **counts,
            "stage": "job_pipeline_matching",
            "status": "deadline" if counts["deadline_reached"] else "completed",
            "reason_code": "deadline" if counts["deadline_reached"] else "none",
        },
    )
    if not counts["deadline_reached"]:
        if (
            counts["sources_total"]
            and not successful_sources
            and not counts["sources_skipped"]
        ):
            raise JobPipelineError("all approved job sources failed", counts)
        if counts["users_total"] and not successful_users:
            raise JobPipelineError("all eligible users failed", counts)
    return counts


__all__ = ["COUNT_KEYS", "JobPipelineError", "run_job_pipeline"]
