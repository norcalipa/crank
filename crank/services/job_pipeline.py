# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Bounded orchestration for periodic job ingestion and user matching."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, Mapping

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from crank.agents.jobs.base import JobSourceQuery
from crank.agents.jobs.employer import resolve_employer
from crank.agents.jobs.ingest import JobIngestResult
from crank.agents.jobs.match_persist import persist_matches
from crank.agents.jobs.matching import project_criteria, rank_listings
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG
from crank.models.agent_run import AgentRun
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.preference import UserPreference, default_preferences
from crank.models.publication import PublicationEvent
from crank.services import agent_runs, publication
from crank.services.job_ingest import ingest_job_source

logger = logging.getLogger(__name__)

COUNT_KEYS = (
    "sources_total",
    "sources_succeeded",
    "sources_failed",
    "sources_skipped",
    "listings_ingested",
    "listings_updated",
    "employers_resolved",
    "employers_unresolved",
    "users_total",
    "users_succeeded",
    "users_failed",
    "matches_persisted",
    "deadline_reached",
)


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


def _record_source_publication(source, result, resolved, unresolved):
    """Record bounded listing publication events for one ingested source.

    One event per bounded chunk of the source's deduplicated employer
    organization ids (never per listing): the sweep unions affected keys
    across pending events, so a source mapped to more organizations than one
    payload chunk holds still publishes every organization — no id is ever
    silently dropped — and downstream recompute (#462) can consume the
    events without per-row event explosion. Must run inside the same
    transaction as the source's accepted writes so the events commit exactly
    when the writes commit.
    """
    organization_ids = list(
        JobListing.all_objects.filter(
            source=source, organization__isnull=False
        )
        # order_by() clears the model's default ordering, which would
        # otherwise add last_seen_at/id to the SELECT and defeat DISTINCT.
        .order_by()
        .values_list("organization_id", flat=True)
        .distinct()
    )
    bound = publication.MAX_PAYLOAD_ORGANIZATION_IDS
    chunks = [
        organization_ids[index : index + bound]
        for index in range(0, len(organization_ids), bound)
    ] or [[]]
    for chunk_index, organization_ids_chunk in enumerate(chunks):
        publication.record_event(
            target_type=PublicationEvent.TargetType.LISTING,
            target_id=source.pk,
            event_kind=PublicationEvent.EventKind.INGESTED,
            payload={
                "source_key": source.adapter_key,
                "ingested": int(result.ingested),
                "updated": int(result.updated),
                "resolved": resolved,
                "unresolved": unresolved,
                "organization_ids": organization_ids_chunk,
                "chunk_index": chunk_index,
                "chunk_count": len(chunks),
            },
        )


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


def _active_listings(limit: int):
    return list(
        JobListing.objects.filter(
            source__approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            source__enabled=True,
        )
        .select_related("organization")
        .order_by("-last_seen_at", "pk")[:limit]
    )


def _run_user(
    user_preference: UserPreference,
    listings: list[Any],
    options: Mapping[str, Any],
) -> int:
    criteria = project_criteria(
        user_preference.preferences,
        user_preference.schema_version,
    )
    config = options.get("ranking_config") or DEFAULT_CONFIG
    # rank_listings is deliberately called here so ranking is an explicit
    # pipeline phase; persist_matches repeats the deterministic calculation as
    # its write-time safety check.
    rank_listings(listings, criteria, config)
    return persist_matches(user_preference.user, listings, criteria, config)


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
    max_listings = max(
        1,
        int(_setting(options, "JOB_PIPELINE_MAX_LISTINGS_PER_USER", 500)),
    )

    sources = list(
        JobSourceCatalog.objects.filter(
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        ).order_by("pk")[:max_sources]
    )
    counts["sources_total"] = len(sources)
    successful_sources = 0
    for source in sources:
        if deadline.reached():
            counts["deadline_reached"] = True
            break
        before_ids = set(
            JobListing.all_objects.filter(source=source).values_list("pk", flat=True)
        )
        try:
            result, resolved, unresolved, skipped = _ingest_source(
                source, options, before_ids
            )
            if skipped:
                counts["sources_skipped"] += 1
                continue
            # One transaction per source stage: the accepted writes and their
            # publication events commit together, so an outbox insert failure
            # (or any crash inside the block) rolls the writes back and
            # committed data can never be left without its event. A source
            # with partial row failures still publishes the rows it accepted.
            with transaction.atomic():
                if int(result.ingested) or int(result.updated) or resolved or unresolved:
                    _record_source_publication(source, result, resolved, unresolved)
            counts["listings_ingested"] += int(result.ingested)
            counts["listings_updated"] += int(result.updated)
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
                    },
                )
        except Exception as exc:  # noqa: BLE001 - isolate source failures
            counts["sources_failed"] += 1
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

    preferences = _eligible_preferences()[:max_users]
    counts["users_total"] = len(preferences)
    successful_users = 0
    if deadline.reached():
        counts["deadline_reached"] = True
    listings = [] if counts["deadline_reached"] else _active_listings(max_listings)
    for preference in preferences:
        if deadline.reached():
            counts["deadline_reached"] = True
            break
        try:
            counts["matches_persisted"] += int(_run_user(preference, listings, options))
            counts["users_succeeded"] += 1
            successful_users += 1
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
