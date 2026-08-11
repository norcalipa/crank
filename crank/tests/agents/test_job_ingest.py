# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Offline tests for normalized job ingestion orchestration."""

from datetime import timedelta

from django.test import TestCase
from unittest.mock import patch

from crank.agents.jobs.errors import UnapprovedJobSource
from django.utils import timezone

from crank.agents.jobs.base import JobSourceQuery, JobSourceResult, RawJobListing
from crank.agents.jobs.ingest import ingest_jobs
from crank.agents.sources.errors import SourceTimeoutError
from crank.models.job import JobListing, JobSourceCatalog


def make_source(name="Ingest fixtures"):
    return JobSourceCatalog.objects.create(
        name=name,
        adapter_key="fixture-adapter",
        base_url="https://jobs.example.test",
        approval_state=JobSourceCatalog.ApprovalState.APPROVED,
        enabled=True,
    )


def raw(external_id="fixture-1", **changes):
    now = timezone.now()
    values = {
        "external_id": external_id,
        "canonical_url": f"https://jobs.example.test/{external_id}",
        "employer_name": "Fixture Employer",
        "title": "Fixture Engineer",
        "location_text": "Remote",
        "first_seen_at": now,
        "last_seen_at": now,
        "source_metadata": {"fixture": True},
    }
    values.update(changes)
    return RawJobListing(**values)


class StubAdapter:
    def __init__(self, listings=(), error=None):
        self.listings = tuple(listings)
        self.error = error

    def fetch(self, query):
        if self.error:
            raise self.error
        return JobSourceResult(
            listings=self.listings,
            pages_fetched=1,
            items_seen=len(self.listings),
        )


class JobIngestTests(TestCase):
    def test_first_ingest_and_replay_are_idempotent(self):
        source = make_source()
        first = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw()]))
        replay = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw()]))
        assert first.ingested == 1
        assert first.updated == 0
        assert replay.ingested == 0
        assert replay.updated == 1
        assert JobListing.all_objects.filter(source=source).count() == 1

    def test_updates_freshness_and_terminal_states(self):
        source = make_source()
        original = raw()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([original]))
        newer = raw(
            title="Updated Fixture Engineer",
            last_seen_at=original.last_seen_at + timedelta(hours=1),
            first_seen_at=original.first_seen_at + timedelta(hours=1),
        )
        result = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([newer]))
        listing = JobListing.all_objects.get(source=source, external_id="fixture-1")
        assert result.updated == 1
        assert listing.title == "Updated Fixture Engineer"
        assert listing.first_seen_at == original.first_seen_at
        assert listing.last_seen_at == newer.last_seen_at

        closed = raw(
            status=JobListing.Status.CLOSED,
            last_seen_at=newer.last_seen_at + timedelta(hours=1),
        )
        result = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([closed]))
        listing.refresh_from_db()
        assert result.closed == 1
        assert listing.status == JobListing.Status.CLOSED

        expired = raw(
            external_id="fixture-2",
            status=JobListing.Status.EXPIRED,
        )
        result = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([expired]))
        assert result.ingested == 1
        assert result.expired == 1

    def test_fetch_errors_are_typed_and_sanitized(self):
        source = make_source()
        result = ingest_jobs(
            source,
            JobSourceQuery(),
            adapter=StubAdapter(error=SourceTimeoutError("secret payload must not leak")),
        )
        assert result.errors == 1
        assert "SourceTimeoutError" in result.error_summary
        assert "secret" not in result.error_summary
        assert result.pages_fetched == 0

    def test_listing_errors_do_not_abort_other_listings(self):
        source = make_source()
        invalid = raw(external_id="fixture-invalid")
        original_ingest = JobListing.ingest

        def ingest_with_one_error(source_obj, listing):
            if listing.external_id == "fixture-invalid":
                raise UnapprovedJobSource("unapproved fixture URL")
            return original_ingest(source_obj, listing)

        with patch.object(JobListing, "ingest", side_effect=ingest_with_one_error):
            result = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw(), invalid]))
        assert result.ingested == 1
        assert result.errors == 1
        assert JobListing.all_objects.filter(source=source).count() == 1

    def test_ingest_result_total_property(self):
        """JobIngestResult.total sums ingested + updated (line 30)."""
        from crank.agents.jobs.ingest import JobIngestResult

        result = JobIngestResult(ingested=3, updated=2)
        assert result.total == 5

    def test_status_change_detected_as_update(self):
        """_changed returns True when status differs and is not terminal→active (lines 60-65)."""
        source = make_source()
        original = raw()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([original]))
        # Active → Expired is a status change that should be detected
        expired = raw(
            status=JobListing.Status.EXPIRED,
            last_seen_at=original.last_seen_at + timedelta(hours=1),
        )
        result = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([expired]))
        listing = JobListing.all_objects.get(source=source, external_id="fixture-1")
        assert result.updated == 1
        assert listing.status == JobListing.Status.EXPIRED

    def test_status_change_with_matching_timestamps(self):
        """_changed returns True for status change with same timestamps (line 64)."""
        from crank.agents.jobs.ingest import _changed

        source = make_source()
        original = raw()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([original]))
        listing = JobListing.all_objects.get(source=source, external_id="fixture-1")
        # Same timestamps, different status (active→expired) — should hit line 64
        expired_raw = raw(
            status=JobListing.Status.EXPIRED,
            last_seen_at=listing.last_seen_at,
            first_seen_at=listing.first_seen_at,
        )
        assert _changed(listing, expired_raw) is True

    def test_terminal_to_active_not_counted_as_changed(self):
        """_changed returns False for terminal→active when timestamps match (lines 60-65)."""
        from crank.agents.jobs.ingest import _changed

        source = make_source()
        original = raw()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([original]))
        listing = JobListing.all_objects.get(source=source, external_id="fixture-1")
        # Simulate terminal state
        listing.status = JobListing.Status.CLOSED
        listing.save(update_fields=["status"])
        # Raw says active again with SAME timestamp — _changed should return False
        # because terminal→active is explicitly excluded from status-change detection.
        active_raw = raw(
            last_seen_at=listing.last_seen_at,
            first_seen_at=listing.first_seen_at,
        )
        assert _changed(listing, active_raw) is False
