# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Orchestration for fetching and persisting normalized job listings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crank.agents.jobs.base import JobSourceQuery
from crank.agents.jobs.registry import build_job_adapter
from crank.models.job import JobListing


@dataclass(frozen=True)
class JobIngestResult:
    """Sanitized counts and error information for one ingestion attempt."""

    ingested: int = 0
    updated: int = 0
    closed: int = 0
    expired: int = 0
    errors: int = 0
    pages_fetched: int = 0
    items_seen: int = 0
    error_summary: str = ""

    @property
    def total(self) -> int:
        return self.ingested + self.updated


def _safe_error(exc: Exception) -> str:
    category = getattr(exc, "category", "permanent")
    return f"{exc.__class__.__name__} ({category})"


def _existing_listing(source: Any, raw: Any):
    listing = JobListing.all_objects.filter(source=source, external_id=raw.external_id).first()
    if listing is None:
        listing = JobListing.all_objects.filter(source=source, canonical_url=raw.canonical_url).first()
    return listing


def _changed(existing: Any, raw: Any) -> bool:
    if existing is None:
        return False
    fields = (
        "canonical_url", "employer_name", "employer_domain", "title",
        "location_text", "is_remote", "compensation_min", "compensation_max",
        "compensation_currency", "compensation_interval", "description_excerpt",
        "source_metadata",
    )
    for field in fields:
        if getattr(existing, field) != getattr(raw, field):
            return True
    observed_at = raw.last_seen_at or raw.first_seen_at
    if observed_at and observed_at > existing.last_seen_at:
        return True
    if raw.status != existing.status and not (
        existing.status in {JobListing.Status.CLOSED, JobListing.Status.EXPIRED}
        and raw.status == JobListing.Status.ACTIVE
    ):
        return True
    return False


def ingest_jobs(source: Any, query: JobSourceQuery, *, adapter=None) -> JobIngestResult:
    """Fetch ``source`` and upsert each raw listing.

    The adapter and model boundaries perform validation.  This service does
    not log or retain exception text because source payloads are untrusted.
    Fetch failures are represented in the typed result so a scheduler can
    distinguish a failed run without losing sanitized counters.
    """

    try:
        adapter = adapter or build_job_adapter(source)
        fetched = adapter.fetch(query)
    except Exception as exc:
        return JobIngestResult(errors=1, error_summary=_safe_error(exc))

    ingested = updated = closed = expired = errors = 0
    summaries: list[str] = []
    for raw in fetched.listings:
        try:
            existing = _existing_listing(source, raw)
            changed = _changed(existing, raw)
            listing = JobListing.ingest(source, raw)
            if existing is None:
                ingested += 1
            elif changed:
                updated += 1
            if listing.status == JobListing.Status.CLOSED:
                closed += 1
            elif listing.status == JobListing.Status.EXPIRED:
                expired += 1
        except Exception as exc:
            errors += 1
            summaries.append(_safe_error(exc))

    return JobIngestResult(
        ingested=ingested,
        updated=updated,
        closed=closed,
        expired=expired,
        errors=errors,
        pages_fetched=fetched.pages_fetched,
        items_seen=fetched.items_seen,
        error_summary=", ".join(summaries),
    )


__all__ = ["JobIngestResult", "ingest_jobs"]
