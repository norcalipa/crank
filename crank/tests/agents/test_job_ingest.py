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
from crank.agents.sources.errors import (
    SourceServerError,
    SourceThrottledError,
    SourceTimeoutError,
)
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
        # AC-1 (issue #469): an unchanged replay is neither created nor updated.
        assert replay.ingested == 0
        assert replay.updated == 0
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

    def test_resolver_failure_does_not_suppress_committed_upsert(self):
        """Issue #469 review MAJOR: a resolver exception after a committed
        listing upsert must not suppress the update counters or ``seen_ids``.
        The accepted title change still reports ``updated`` (so the manual
        crawl still publishes), the failure is tallied as unresolved/error,
        and closure stays default-deny."""
        source = make_source()
        original = raw()
        ingest_jobs(source, JobSourceQuery(), adapter=CompleteStubAdapter([original], complete=True))
        changed = raw(
            title="Committed New Title",
            last_seen_at=original.last_seen_at + timedelta(hours=1),
        )
        with patch(
            "crank.agents.jobs.employer.resolve_employer",
            side_effect=RuntimeError("resolver exploded"),
        ):
            result = ingest_jobs(
                source,
                JobSourceQuery(),
                adapter=CompleteStubAdapter([changed], complete=True),
            )
        assert result.updated == 1
        assert result.errors == 1
        assert result.unresolved == 1
        assert result.absent_closed == 0
        assert result.closure_skipped_reason == "listing_errors"
        listing = JobListing.all_objects.get(source=source, external_id="fixture-1")
        assert listing.title == "Committed New Title"

    def test_listing_errors_do_not_abort_other_listings(self):
        source = make_source()
        invalid = raw(external_id="fixture-invalid")
        original_ingest = JobListing.ingest_with_outcome

        def ingest_with_one_error(source_obj, listing):
            if listing.external_id == "fixture-invalid":
                raise UnapprovedJobSource("unapproved fixture URL")
            return original_ingest(source_obj, listing)

        with patch.object(JobListing, "ingest_with_outcome", side_effect=ingest_with_one_error):
            result = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw(), invalid]))
        assert result.ingested == 1
        assert result.errors == 1
        assert JobListing.all_objects.filter(source=source).count() == 1

    def test_ingest_result_total_property(self):
        """JobIngestResult.total sums ingested + updated (line 30)."""
        from crank.agents.jobs.ingest import JobIngestResult

        result = JobIngestResult(ingested=3, updated=2)
        assert result.total == 5

    def test_ingest_tallies_resolved_and_unresolved_counts(self):
        """Issue #469 review MAJOR: ``ingest_jobs`` resolves employers on
        every replay, so it tallies resolved/unresolved counts for the caller
        (the manual crawl previously hard-coded 0, 0)."""
        source = make_source()

        class Resolution:
            def __init__(self, resolved):
                self.resolved = resolved

        with patch(
            "crank.agents.jobs.employer.resolve_employer",
            side_effect=lambda listing: Resolution(listing.external_id.startswith("yes")),
        ):
            result = ingest_jobs(
                source,
                JobSourceQuery(),
                adapter=StubAdapter([raw(external_id="yes-1"), raw(external_id="no-1")]),
            )
        assert result.resolved == 1
        assert result.unresolved == 1

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
        """Same-timestamp terminal observation still counts as an update."""
        source = make_source()
        original = raw()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([original]))
        listing = JobListing.all_objects.get(source=source, external_id="fixture-1")
        expired_raw = raw(
            status=JobListing.Status.EXPIRED,
            last_seen_at=listing.last_seen_at,
            first_seen_at=listing.first_seen_at,
        )
        result = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([expired_raw]))
        assert result.updated == 1
        listing.refresh_from_db()
        assert listing.status == JobListing.Status.EXPIRED

    def test_terminal_to_active_not_counted_as_changed(self):
        """Terminal listings are never resurrected by an active observation."""
        source = make_source()
        original = raw()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([original]))
        listing = JobListing.all_objects.get(source=source, external_id="fixture-1")
        listing.status = JobListing.Status.CLOSED
        listing.save(update_fields=["status"])
        active_raw = raw(
            last_seen_at=listing.last_seen_at,
            first_seen_at=listing.first_seen_at,
        )
        result = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([active_raw]))
        assert result.updated == 0
        listing.refresh_from_db()
        assert listing.status == JobListing.Status.CLOSED


class CompleteStubAdapter(StubAdapter):
    def __init__(self, listings=(), error=None, complete=True, truncated=False):
        super().__init__(listings, error)
        self.complete = complete
        self.truncated = truncated

    def fetch(self, query):
        if self.error:
            raise self.error
        return JobSourceResult(
            listings=self.listings,
            pages_fetched=1,
            items_seen=len(self.listings),
            complete_snapshot=self.complete,
            truncated=self.truncated,
        )


class AbsenceClosureTests(TestCase):
    """Issue #469 AC-7/8/9: closure only behind a proven complete snapshot."""

    def test_complete_snapshot_closes_absent_active_listings(self):
        source = make_source()
        present = raw()
        absent = raw(external_id="fixture-absent")
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([present, absent]))
        result = ingest_jobs(
            source, JobSourceQuery(), adapter=CompleteStubAdapter([present])
        )
        assert result.absent_closed == 1
        assert result.closure_skipped_reason == ""
        assert result.complete_snapshot is True
        listing = JobListing.all_objects.get(source=source, external_id="fixture-absent")
        assert listing.status == JobListing.Status.CLOSED
        kept = JobListing.all_objects.get(source=source, external_id="fixture-1")
        assert kept.status == JobListing.Status.ACTIVE

    def test_incomplete_snapshot_closes_nothing(self):
        source = make_source()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw()]))
        result = ingest_jobs(
            source,
            JobSourceQuery(),
            adapter=CompleteStubAdapter([], complete=False),
        )
        assert result.absent_closed == 0
        assert result.closure_skipped_reason == "incomplete_snapshot"
        assert JobListing.all_objects.filter(
            source=source, status=JobListing.Status.ACTIVE
        ).count() == 1

    def test_truncated_snapshot_closes_nothing(self):
        source = make_source()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw()]))
        result = ingest_jobs(
            source,
            JobSourceQuery(),
            adapter=CompleteStubAdapter([], complete=True, truncated=True),
        )
        assert result.absent_closed == 0
        assert result.closure_skipped_reason == "truncated"

    def test_filtered_query_skips_source_wide_closure(self):
        """Issue #469 review: a complete filtered result is completeness
        for that filter, not the whole source, so a keyword/location query
        must never close rows outside the filter."""
        source = make_source()
        ingest_jobs(
            source, JobSourceQuery(), adapter=StubAdapter([raw(), raw(external_id="fixture-other")])
        )
        result = ingest_jobs(
            source,
            JobSourceQuery(keyword="engineer"),
            adapter=CompleteStubAdapter([raw()], complete=True),
        )
        assert result.absent_closed == 0
        assert result.closure_skipped_reason == "filtered_query"
        assert JobListing.all_objects.filter(
            source=source, status=JobListing.Status.ACTIVE
        ).count() == 2

    def test_fetch_error_sets_closure_skip_reason(self):
        """Issue #469 review: a fetch exception reports a low-cardinality
        skip reason instead of an empty string."""
        source = make_source()
        result = ingest_jobs(
            source,
            JobSourceQuery(),
            adapter=StubAdapter(error=SourceTimeoutError("timeout")),
        )
        assert result.errors == 1
        assert result.closure_skipped_reason == "fetch_error"

    def test_truncated_reports_truncated_not_incomplete(self):
        """Issue #469 review: truncation is reported before the generic
        incomplete-snapshot reason, even though a truncated adapter also
        leaves complete_snapshot False."""
        source = make_source()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw()]))
        result = ingest_jobs(
            source,
            JobSourceQuery(),
            adapter=CompleteStubAdapter([], complete=False, truncated=True),
        )
        assert result.absent_closed == 0
        assert result.closure_skipped_reason == "truncated"

    def test_catalog_kill_switch_disables_closure(self):
        source = make_source()
        source.catalog_metadata = {"supports_complete_snapshot": False}
        source.save()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw()]))
        result = ingest_jobs(
            source, JobSourceQuery(), adapter=CompleteStubAdapter([])
        )
        assert result.absent_closed == 0
        assert result.closure_skipped_reason == "disabled_by_catalog"

    def test_fetch_failure_closes_nothing_and_leaves_rows_untouched(self):
        source = make_source()
        first = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw()]))
        assert first.ingested == 1
        before = JobListing.all_objects.get(source=source, external_id="fixture-1")
        before_seen = before.last_seen_at
        for error in (
            SourceThrottledError("rate limited"),
            SourceServerError("upstream 500"),
            SourceTimeoutError("timeout"),
        ):
            with self.subTest(error=type(error).__name__):
                result = ingest_jobs(
                    source, JobSourceQuery(), adapter=StubAdapter(error=error)
                )
                assert result.errors == 1
                assert result.absent_closed == 0
                assert result.complete_snapshot is False
                after = JobListing.all_objects.get(pk=before.pk)
                assert after.status == JobListing.Status.ACTIVE
                assert after.last_seen_at == before_seen

    def test_explicit_terminal_observation_is_honoured(self):
        source = make_source()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw()]))
        closed = raw(
            status=JobListing.Status.CLOSED,
            last_seen_at=timezone.now() + timedelta(hours=1),
        )
        result = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([closed]))
        listing = JobListing.all_objects.get(source=source, external_id="fixture-1")
        assert listing.status == JobListing.Status.CLOSED
        assert result.closed == 1
        # A later active observation cannot resurrect it (AC-10).
        active = raw(last_seen_at=timezone.now() + timedelta(hours=2))
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([active]))
        listing.refresh_from_db()
        assert listing.status == JobListing.Status.CLOSED

    def test_mixed_and_missing_external_id_replay_is_stable(self):
        source = make_source()
        identified = raw()
        anonymous = raw(
            external_id="", canonical_url="https://jobs.example.test/anon-1"
        )
        first = ingest_jobs(
            source, JobSourceQuery(), adapter=StubAdapter([identified, anonymous])
        )
        assert first.ingested == 2
        replay = ingest_jobs(
            source, JobSourceQuery(), adapter=StubAdapter([identified, anonymous])
        )
        assert replay.ingested == 0
        assert replay.updated == 0
        assert JobListing.all_objects.filter(source=source).count() == 2


class ListingErrorClosureTests(TestCase):
    def test_listing_errors_skip_closure(self):
        source = make_source()
        ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw()]))
        invalid = raw(external_id="fixture-invalid")

        def ingest_with_one_error(source_obj, listing):
            if listing.external_id == "fixture-invalid":
                raise UnapprovedJobSource("unapproved fixture URL")
            return JobListing.ingest_with_outcome(source_obj, listing)

        with patch.object(
            JobListing, "ingest_with_outcome", side_effect=ingest_with_one_error
        ):
            result = ingest_jobs(
                source,
                JobSourceQuery(),
                adapter=CompleteStubAdapter([invalid], complete=True),
            )
        assert result.errors == 1
        assert result.absent_closed == 0
        assert result.closure_skipped_reason == "listing_errors"
