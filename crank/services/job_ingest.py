# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Single-owner ingestion boundary for job sources (issue #462).

Manual (``trigger_crawl``) and recurring (``run_job_pipeline``) invocations
share this one idempotent service boundary: it enforces the approved+enabled
source policy, acquires a per-source MySQL advisory lock (generalizing the
run-type ``GET_LOCK`` guard to arbitrary lock names), and calls ``ingest_jobs``
exactly once per lock holder. A concurrent second path that cannot take the
lock skips the source with a recorded, sanitized reason instead of fetching or
publishing it a second time; combined with ``ingest_jobs``' upsert identity,
replay is idempotent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from crank.agents.jobs.base import JobSourceQuery
from crank.agents.jobs.ingest import JobIngestResult, ingest_jobs
from crank.models.agent_run import (
    acquire_named_advisory_lock,
    release_named_advisory_lock,
    source_lock_name,
)
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.publication import PublicationEvent
from crank.services import monitoring, publication

logger = logging.getLogger(__name__)

# Low-cardinality reason code emitted when the per-source lock is held by
# another ingestion path. Never carries source payloads or credentials.
SKIP_OVERLAP = "overlap_lock"


class JobSourcePolicyError(ValueError):
    """Raised when a job source is not approved+enabled+configured."""


@dataclass(frozen=True)
class JobSourceIngestion:
    """Outcome of one boundary-mediated ingestion attempt.

    ``result`` is ``None`` exactly when ``skipped`` is ``True``; ``reason`` is
    a low-cardinality code (empty when the source was ingested).
    """

    result: JobIngestResult | None
    skipped: bool
    reason: str


def _policy_check(source: Any) -> None:
    """Enforce the same source policy as manual crawl requests."""
    if source.approval_state != JobSourceCatalog.ApprovalState.APPROVED:
        raise JobSourcePolicyError("source is not approved")
    if not source.enabled:
        raise JobSourcePolicyError("source is disabled")
    if not str(getattr(source, "adapter_key", "") or "").strip():
        raise JobSourcePolicyError("source adapter is not configured")


def record_source_publication(source, result, resolved, unresolved, before_org_ids):
    """Record bounded listing publication events for one ingested source.

    One event per bounded chunk of the deduplicated affected organization
    ids (never per listing): the sweep unions affected keys across pending
    events, so a source mapped to more organizations than one payload chunk
    holds still publishes every organization — no id is ever silently
    dropped. The affected set is the union of the source's pre-stage
    organization ids and its post-stage ones: a listing reassigned from
    organization A to B (or unresolved away from A, or closed by absence
    closure) makes A affected even though it no longer maps to any of the
    source's listings. Must run inside the same transaction as the source's
    accepted writes so the events commit exactly when the writes commit.

    This lives in the shared single-owner boundary (issue #462) so every
    consumer — the recurring ``run_job_pipeline`` and the manual
    ``crawl_runs`` trigger — publishes lifecycle writes identically
    (issue #469 review).
    """
    organization_ids = set(
        JobListing.all_objects.filter(
            source=source, organization__isnull=False
        )
        # order_by() clears the model's default ordering, which would
        # otherwise add last_seen_at/id to the SELECT and defeat DISTINCT.
        .order_by()
        .values_list("organization_id", flat=True)
        .distinct()
    )
    organization_ids.update(before_org_ids)
    organization_ids = sorted(organization_ids)
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


def ingest_job_source(source: Any, *, query: JobSourceQuery, adapter: Any = None) -> JobSourceIngestion:
    """Ingest one approved+enabled job source exactly once per lock holder.

    The per-source advisory lock (``job_source:{pk}``) serializes the fetch
    against every other path routed through this boundary. It is a no-op on
    backends without MySQL advisory locks, where the caller's other guards
    (unique constraints) apply; the ingestion itself stays idempotent through
    ``ingest_jobs``' upsert identity either way.
    """
    _policy_check(source)
    lock_name = source_lock_name(source)
    if not acquire_named_advisory_lock(lock_name, timeout_seconds=0):
        monitoring.record_event(
            "source_stage",
            {
                "stage": "job_ingest",
                "source_key": str(source.adapter_key),
                "status": "skipped",
                "reason_code": SKIP_OVERLAP,
            },
        )
        logger.warning(
            "job source ingestion skipped: source_id=%s reason=%s",
            source.pk,
            SKIP_OVERLAP,
        )
        return JobSourceIngestion(result=None, skipped=True, reason=SKIP_OVERLAP)
    try:
        result = ingest_jobs(source, query, adapter=adapter)
    finally:
        release_named_advisory_lock(lock_name)
    return JobSourceIngestion(result=result, skipped=False, reason="")


__all__ = [
    "JobSourceIngestion",
    "JobSourcePolicyError",
    "SKIP_OVERLAP",
    "ingest_job_source",
    "record_source_publication",
]
