# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Orchestration for fetching and persisting normalized job listings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crank.agents.jobs.base import (
    CATALOG_SUPPORTS_COMPLETE_SNAPSHOT_KEY,
    JobSourceQuery,
)
from crank.agents.jobs.registry import build_job_adapter
from crank.models.job import (
    OUTCOME_CREATED,
    OUTCOME_UPDATED,
    JobListing,
)


@dataclass(frozen=True)
class JobIngestResult:
    """Sanitized counts and error information for one ingestion attempt."""

    ingested: int = 0
    updated: int = 0
    closed: int = 0
    expired: int = 0
    errors: int = 0
    resolved: int = 0
    unresolved: int = 0
    pages_fetched: int = 0
    items_seen: int = 0
    error_summary: str = ""
    absent_closed: int = 0
    complete_snapshot: bool = False
    truncated: bool = False
    closure_skipped_reason: str = ""

    @property
    def total(self) -> int:
        return self.ingested + self.updated


def _safe_error(exc: Exception) -> str:
    category = getattr(exc, "category", "permanent")
    return f"{exc.__class__.__name__} ({category})"


# Low-cardinality absence-closure skip reasons (issue #469). Closure is
# default-deny: it runs only when the fetch succeeded, the adapter asserts a
# complete, untruncated, unfiltered snapshot, and the operator has not
# disabled it.
SKIP_TRUNCATED = "truncated"
SKIP_FETCH_ERROR = "fetch_error"
SKIP_LISTING_ERRORS = "listing_errors"
SKIP_INCOMPLETE_SNAPSHOT = "incomplete_snapshot"
SKIP_FILTERED_QUERY = "filtered_query"
SKIP_DISABLED_BY_CATALOG = "disabled_by_catalog"


def _closure_decision(source: Any, fetched: Any, query: JobSourceQuery, errors: int) -> tuple[bool, str]:
    """Return ``(may_close, skipped_reason)`` for absence-based closure.

    Order matters (issue #469 review): truncation and listing errors are
    reported before the generic incomplete-snapshot reason, and a filtered
    query can never drive source-wide absence closure because a complete
    filtered result is completeness for that filter, not the whole source.
    """
    catalog_metadata = getattr(source, "catalog_metadata", None) or {}
    if bool(getattr(fetched, "truncated", False)):
        return False, SKIP_TRUNCATED
    if errors:
        return False, SKIP_LISTING_ERRORS
    if not bool(getattr(fetched, "complete_snapshot", False)):
        return False, SKIP_INCOMPLETE_SNAPSHOT
    if bool(getattr(query, "keyword", "") or getattr(query, "location", "")):
        return False, SKIP_FILTERED_QUERY
    if catalog_metadata.get(CATALOG_SUPPORTS_COMPLETE_SNAPSHOT_KEY) is False:
        return False, SKIP_DISABLED_BY_CATALOG
    return True, ""


def ingest_jobs(source: Any, query: JobSourceQuery, *, adapter=None) -> JobIngestResult:
    """Fetch ``source`` and upsert each raw listing.

    The adapter and model boundaries perform validation.  This service does
    not log or retain exception text because source payloads are untrusted.
    Fetch failures are represented in the typed result so a scheduler can
    distinguish a failed run without losing sanitized counters. A failed
    fetch returns zero listings with ``complete_snapshot=False``, so a 429,
    5xx, or timeout can never be read as "every absent posting closed".
    """

    try:
        adapter = adapter or build_job_adapter(source)
        fetched = adapter.fetch(query)
    except Exception as exc:
        return JobIngestResult(
            errors=1,
            error_summary=_safe_error(exc),
            closure_skipped_reason=SKIP_FETCH_ERROR,
        )

    ingested = updated = closed = expired = errors = resolved = unresolved = 0
    summaries: list[str] = []
    seen_ids: set[int] = set()
    for raw in fetched.listings:
        try:
            listing, outcome = JobListing.ingest_with_outcome(source, raw)
            # Employer resolution is deliberately separate from listing
            # identity/upsert: a corrected reviewed alias can reprocess the
            # same listing without creating another row. The resolution
            # outcome is tallied so the caller can report the actual
            # resolved/unresolved counts (issue #469 review).
            from crank.agents.jobs.employer import resolve_employer
            if resolve_employer(listing).resolved:
                resolved += 1
            else:
                unresolved += 1
            seen_ids.add(listing.pk)
            if outcome == OUTCOME_CREATED:
                ingested += 1
            elif outcome == OUTCOME_UPDATED:
                updated += 1
            if listing.status == JobListing.Status.CLOSED:
                closed += 1
            elif listing.status == JobListing.Status.EXPIRED:
                expired += 1
        except Exception as exc:
            errors += 1
            summaries.append(_safe_error(exc))

    may_close, skip_reason = _closure_decision(source, fetched, query, errors)
    absent_closed = (
        JobListing.all_objects.close_absent(source, seen_ids) if may_close else 0
    )
    closed += absent_closed

    return JobIngestResult(
        ingested=ingested,
        updated=updated,
        closed=closed,
        expired=expired,
        errors=errors,
        resolved=resolved,
        unresolved=unresolved,
        pages_fetched=fetched.pages_fetched,
        items_seen=fetched.items_seen,
        error_summary=", ".join(summaries),
        absent_closed=absent_closed,
        complete_snapshot=bool(getattr(fetched, "complete_snapshot", False)),
        truncated=bool(getattr(fetched, "truncated", False)),
        closure_skipped_reason=skip_reason,
    )


__all__ = ["JobIngestResult", "ingest_jobs"]
