# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from crank.admin import JobListingAdmin, JobSourceCatalogAdmin
from crank.agents.jobs.base import (
    JobSourceAdapter,
    JobSourceQuery,
    RawJobListing,
    _validate_compensation,
    _validate_domain,
    validate_catalog_metadata,
    validate_job_url,
    validate_source_metadata,
)
from crank.agents.jobs.errors import JobSchemaError
from crank.models.job import JobListing, JobSourceCatalog


def make_source(**kwargs):
    values = {
        "name": "Synthetic jobs",
        "adapter_key": "synthetic.v1",
        "base_url": "https://jobs.example.test",
        "approval_state": JobSourceCatalog.ApprovalState.APPROVED,
        "enabled": True,
    }
    values.update(kwargs)
    return JobSourceCatalog.objects.create(**values)


def raw(**kwargs):
    values = {
        "external_id": "abc-1",
        "canonical_url": "https://jobs.example.test/jobs/abc-1#tracking",
        "employer_name": "Acme <b>Labs</b>",
        "title": "Senior Python Developer",
        "first_seen_at": timezone.now() - timedelta(days=1),
        "last_seen_at": timezone.now(),
    }
    values.update(kwargs)
    return RawJobListing(**values)


class RawJobListingTests(TestCase):
    def test_canonical_url_index_length_fits_mysql_utf8mb4_limit(self):
        field = JobListing._meta.get_field("canonical_url")
        # MySQL permits 3072 bytes per InnoDB index key. The unique index
        # includes source_id (8 bytes) alongside this utf8mb4 field.
        self.assertEqual(field.max_length, 750)
        self.assertLessEqual(field.max_length * 4 + 8, 3072)

    def test_validation_and_text_sanitization(self):
        listing = raw(description_excerpt="<script>alert(1)</script> Build APIs")
        self.assertEqual(listing.employer_name, "Acme Labs")
        self.assertEqual(listing.description_excerpt, "alert(1) Build APIs")
        self.assertEqual(listing.canonical_url, "https://jobs.example.test/jobs/abc-1")

    def test_rejects_bad_url_dates_compensation_and_oversized_fields(self):
        cases = [
            {"canonical_url": "http://jobs.example.test/job/1"},
            {"canonical_url": "https://evil.example/job/1"},
            {"first_seen_at": timezone.now().replace(tzinfo=None)},
            {"compensation_min": -1},
            {"compensation_min": 2, "compensation_max": 1},
            {"title": "x" * 301},
        ]
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(Exception):
                    raw(**values)

    def test_query_bounds(self):
        with self.assertRaises(Exception):
            JobSourceQuery(max_listings=0)
        with self.assertRaises(Exception):
            JobSourceQuery(max_pages=1001)

    def test_rejects_invalid_types_and_url_forms(self):
        invalid_values = [
            {"external_id": 42},
            {"employer_name": ""},
            {"title": ""},
            {"employer_domain": "not-a-hostname"},
            {"is_remote": "yes"},
            {"status": "draft"},
            {"compensation_min": True},
            {"compensation_min": float("inf")},
            {"compensation_min": Decimal("NaN")},
            {"first_seen_at": timezone.now(), "last_seen_at": timezone.now() - timedelta(days=1)},
        ]
        for values in invalid_values:
            with self.subTest(values=values):
                with self.assertRaises(JobSchemaError):
                    raw(**values)

        with self.assertRaises(JobSchemaError):
            validate_job_url(None)
        with self.assertRaises(JobSchemaError):
            validate_job_url("x" * 1025)
        with self.assertRaises(JobSchemaError):
            validate_job_url("https://jobs.example.test:443/job/1")
        with self.assertRaises(JobSchemaError):
            validate_job_url("https://[::1")

    def test_optional_dates_are_filled_from_each_other_or_now(self):
        only_last = timezone.now()
        listing = raw(first_seen_at=None, last_seen_at=only_last)
        self.assertEqual(listing.first_seen_at, only_last)
        only_first = timezone.now()
        listing = raw(first_seen_at=only_first, last_seen_at=None)
        self.assertEqual(listing.last_seen_at, only_first)
        listing = raw(first_seen_at=None, last_seen_at=None)
        self.assertIsNotNone(listing.first_seen_at)
        self.assertEqual(listing.first_seen_at, listing.last_seen_at)

    def test_domain_and_metadata_validation(self):
        self.assertEqual(_validate_domain(" Example.COM. "), "example.com")
        for value in ("", "example", "example/path", "example:443", "user@example.com"):
            with self.subTest(value=value):
                with self.assertRaises(JobSchemaError):
                    _validate_domain(value)

        with self.assertRaises(JobSchemaError):
            validate_source_metadata(1)
        with self.assertRaises(JobSchemaError):
            validate_source_metadata({"api_token": "secret"})
        with self.assertRaises(JobSchemaError):
            validate_source_metadata({"nested": [{"response_body": "raw"}]})
        with self.assertRaises(JobSchemaError):
            validate_source_metadata({"value": float("nan")})
        with self.assertRaises(JobSchemaError):
            validate_source_metadata({"value": object()})
        with self.assertRaises(JobSchemaError):
            validate_source_metadata({"value": "x" * 9000})
        with self.assertRaises(JobSchemaError):
            validate_source_metadata({("not", "json"): "value"})
        copied = validate_source_metadata({"nested": [{"ok": True}, ("value",)]})
        self.assertEqual(copied["nested"][0]["ok"], True)

    def test_compensation_field_boundaries(self):
        with self.assertRaises(JobSchemaError):
            _validate_compensation(Decimal("1e100"), "compensation_min")
        with self.assertRaises(JobSchemaError):
            _validate_compensation(Decimal("1.001"), "compensation_min")
        with self.assertRaises(JobSchemaError):
            _validate_compensation(Decimal("1000000000000"), "compensation_min")

    def test_catalog_metadata_is_sanitized_and_bounded(self):
        metadata = validate_catalog_metadata(
            {
                "nested": {"items": ["ok", (True, None)]},
                "api_key": "removed",
                "Authorization": "removed",
            }
        )
        self.assertEqual(metadata, {"nested": {"items": ["ok", [True, None]]}})

        with self.assertRaises(JobSchemaError):
            validate_catalog_metadata({1: "non-string key"})
        with self.assertRaises(JobSchemaError):
            validate_catalog_metadata({"value": float("inf")})
        with self.assertRaises(JobSchemaError):
            validate_catalog_metadata({"value": object()})
        with self.assertRaises(JobSchemaError):
            validate_catalog_metadata({"value": "x" * 9000})
        with patch("crank.agents.jobs.base.json.dumps", side_effect=TypeError("not JSON")):
            with self.assertRaises(JobSchemaError):
                validate_catalog_metadata({"value": "ok"})

    def test_query_text_validation_and_abstract_contract(self):
        with self.assertRaises(JobSchemaError):
            JobSourceQuery(keyword=object())
        with self.assertRaises(JobSchemaError):
            JobSourceQuery(location=object())

        class CallsBaseFetch(JobSourceAdapter):
            key = "calls-base"
            version = "1"

            def fetch(self, query):
                return super().fetch(query)

        with self.assertRaises(NotImplementedError):
            CallsBaseFetch(object()).fetch(JobSourceQuery())


class JobListingModelTests(TestCase):
    def test_model_strings_manager_and_presence(self):
        source = make_source()
        self.assertEqual(str(source), "Synthetic jobs")
        listing = JobListing.ingest(source, raw())
        self.assertEqual(str(listing), "Senior Python Developer (Acme Labs)")
        self.assertEqual(JobListing.all_objects.active().get(), listing)
        self.assertTrue(listing.is_presentable)
        listing.status = JobListing.Status.CLOSED
        self.assertFalse(listing.is_presentable)

    def test_ingest_updates_mutable_state_and_retains_provenance(self):
        source = make_source()
        first = raw()
        listing = JobListing.ingest(source, first)
        self.assertEqual(listing.first_seen_at, first.first_seen_at)
        updated = raw(
            title="Staff Python Developer",
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now() + timedelta(hours=1),
            status=JobListing.Status.CLOSED,
            compensation_min=100,
        )
        changed = JobListing.ingest(source, updated)
        self.assertEqual(changed.pk, listing.pk)
        changed.refresh_from_db()
        self.assertEqual(changed.first_seen_at, first.first_seen_at)
        self.assertEqual(changed.title, "Staff Python Developer")
        self.assertEqual(changed.status, JobListing.Status.CLOSED)
        self.assertEqual(changed.compensation_min, Decimal("100.00"))
        self.assertEqual(JobListing.objects.count(), 0)
        self.assertEqual(JobListing.all_objects.count(), 1)

    def test_ingest_does_not_resurrect_terminal_listing(self):
        source = make_source()
        closed = JobListing.ingest(source, raw(status=JobListing.Status.CLOSED))
        observed = raw(
            status=JobListing.Status.ACTIVE,
            title="Updated active title",
            first_seen_at=closed.first_seen_at,
            last_seen_at=closed.last_seen_at + timedelta(hours=1),
        )
        listing = JobListing.ingest(source, observed)
        self.assertEqual(listing.status, JobListing.Status.CLOSED)

    def test_ingest_keeps_state_for_stale_observation(self):
        source = make_source()
        current = raw(status=JobListing.Status.ACTIVE)
        listing = JobListing.ingest(source, current)
        stale = raw(
            status=JobListing.Status.CLOSED,
            title="Stale title",
            first_seen_at=current.first_seen_at - timedelta(hours=1),
            last_seen_at=current.last_seen_at - timedelta(hours=1),
        )
        changed = JobListing.ingest(source, stale)
        self.assertEqual(changed.status, JobListing.Status.ACTIVE)

    def test_ingest_reconciles_concurrent_insert(self):
        source = make_source()
        listing = JobListing.ingest(source, raw())
        incoming = raw(title="Concurrent update", last_seen_at=listing.last_seen_at + timedelta(hours=1))
        with patch.object(JobListing.all_objects, "create", side_effect=IntegrityError):
            changed = JobListing.ingest(source, incoming)
        self.assertEqual(changed.pk, listing.pk)
        self.assertEqual(changed.title, "Concurrent update")

    def test_ingest_reraises_unreconciled_integrity_error(self):
        source = make_source()
        incoming = raw()
        with patch.object(JobListing.all_objects, "create", side_effect=IntegrityError):
            with self.assertRaises(IntegrityError):
                JobListing.ingest(source, incoming)

    def test_canonical_url_fallback_deduplicates_without_external_id(self):
        source = make_source()
        first = JobListing.ingest(source, raw(external_id=""))
        second = JobListing.ingest(source, raw(external_id="new-id", title="Updated"))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(JobListing.all_objects.get(pk=first.pk).external_id, "new-id")

    def test_database_constraints_prevent_duplicate_keys(self):
        source = make_source()
        listing = JobListing.ingest(source, raw())
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JobListing.all_objects.create(
                    source=source, external_id="abc-1", canonical_url="https://jobs.example.test/jobs/other",
                    employer_name="Acme", title="Other", first_seen_at=listing.first_seen_at,
                    last_seen_at=listing.last_seen_at,
                )

    def test_model_clean_rejects_inverted_dates_and_compensation(self):
        source = make_source()
        item = JobListing(
            source=source, external_id="x", canonical_url="https://jobs.example.test/x",
            employer_name="Acme", title="Role", first_seen_at=timezone.now(),
            last_seen_at=timezone.now() - timedelta(days=1), compensation_min=10,
            compensation_max=2,
        )
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_model_clean_validates_url_and_each_compensation_boundary(self):
        source = make_source()
        base = dict(
            source=source,
            external_id="x",
            canonical_url="https://jobs.example.test/x",
            employer_name="Acme",
            title="Role",
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        item = JobListing(**{**base, "canonical_url": "https://evil.example/x"})
        with self.assertRaises(ValidationError):
            item.clean()
        for values in (
            {"compensation_min": -1},
            {"compensation_max": -1},
            {"compensation_min": 10, "compensation_max": 2},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    JobListing(**{**base, **values}).clean()


class JobSourceCatalogModelTests(TestCase):
    def test_clean_and_allowed_hosts(self):
        source = make_source()
        self.assertIn("jobs.example.test", source.allowed_hosts())
        source.clean()
        source.base_url = "https://evil.example/jobs"
        with self.assertRaises(ValidationError):
            source.clean()

        source.base_url = "https://jobs.example.test"
        source.catalog_metadata = {"nested": object()}
        with self.assertRaises(ValidationError):
            source.clean()


class JobAdminAuthorizationTests(TestCase):
    def test_job_admin_is_staff_only_and_listing_content_is_not_in_list(self):
        site = AdminSite()
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, site)
        listing_admin = JobListingAdmin(JobListing, site)
        request = type("Request", (), {"user": User.objects.create_user(username="u")})()
        self.assertFalse(source_admin.has_view_permission(request))
        self.assertFalse(listing_admin.has_view_permission(request))
        self.assertNotIn("description_excerpt", listing_admin.list_display)


class JobIdentityHardeningTests(TestCase):
    """Issue #469: stable identity, regression gating, and absence closure."""

    def test_upsert_reports_created_updated_unchanged(self):
        from crank.models.job import OUTCOME_CREATED, OUTCOME_UNCHANGED, OUTCOME_UPDATED

        source = make_source()
        listing, outcome = JobListing.ingest_with_outcome(source, raw())
        self.assertEqual(outcome, OUTCOME_CREATED)
        replay, outcome = JobListing.ingest_with_outcome(source, raw())
        self.assertEqual(replay.pk, listing.pk)
        self.assertEqual(outcome, OUTCOME_UNCHANGED)
        newer = raw(
            title="Newer Title",
            last_seen_at=timezone.now() + timedelta(hours=1),
        )
        _, outcome = JobListing.ingest_with_outcome(source, newer)
        self.assertEqual(outcome, OUTCOME_UPDATED)

    def test_two_empty_external_id_listings_both_persist(self):
        """AC-2: empty external_id never matches an unrelated listing."""
        source = make_source()
        first = JobListing.ingest(
            source,
            raw(external_id="", canonical_url="https://jobs.example.test/jobs/one"),
        )
        second = JobListing.ingest(
            source,
            raw(external_id="", canonical_url="https://jobs.example.test/jobs/two"),
        )
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(JobListing.all_objects.filter(source=source).count(), 2)
        # A replay of the first is matched by canonical_url, not by "".
        replay = JobListing.ingest(
            source,
            raw(external_id="", canonical_url="https://jobs.example.test/jobs/one"),
        )
        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(JobListing.all_objects.filter(source=source).count(), 2)

    def test_out_of_order_observation_does_not_regress_fields(self):
        """AC-3/AC-4: stale observed_at leaves stored content untouched."""
        source = make_source()
        now = timezone.now()
        current = raw(
            title="Current Title",
            employer_name="Current Employer",
            location_text="Current Location",
            description_excerpt="Current excerpt",
            compensation_min=100,
            compensation_max=200,
            compensation_currency="USD",
            compensation_interval="year",
            first_seen_at=now - timedelta(days=2),
            last_seen_at=now,
            source_metadata={"compensation_basis": "base", "scope": {"countries": ["US"]}},
        )
        listing = JobListing.ingest(source, current)
        stale = raw(
            title="Stale Title",
            employer_name="Stale Employer",
            location_text="Stale Location",
            description_excerpt="Stale excerpt",
            compensation_min=1,
            compensation_max=2,
            compensation_currency="EUR",
            compensation_interval="hour",
            first_seen_at=now - timedelta(days=3),
            last_seen_at=now - timedelta(days=1),
            source_metadata={"compensation_basis": "total"},
        )
        result = JobListing.ingest(source, stale)
        result.refresh_from_db()
        self.assertEqual(result.pk, listing.pk)
        self.assertEqual(result.title, "Current Title")
        self.assertEqual(result.employer_name, "Current Employer")
        self.assertEqual(result.location_text, "Current Location")
        self.assertEqual(result.description_excerpt, "Current excerpt")
        self.assertEqual(result.compensation_min, Decimal("100.00"))
        self.assertEqual(result.compensation_max, Decimal("200.00"))
        self.assertEqual(result.compensation_currency, "USD")
        self.assertEqual(result.compensation_interval, "year")
        self.assertEqual(result.source_metadata["compensation_basis"], "base")
        self.assertEqual(result.source_metadata["scope"], {"countries": ["US"]})
        self.assertEqual(result.last_seen_at, listing.last_seen_at)
        self.assertEqual(result.first_seen_at, current.first_seen_at)

    def test_round_trip_preserves_identity_and_compensation_fields(self):
        """AC-4: canonical URL, timestamps, compensation units/basis, scope."""
        source = make_source()
        metadata = {
            "compensation_basis": "total",
            "scope": {"countries": ["US", "CA"], "role_families": ["engineering"]},
        }
        first = raw(
            compensation_currency="usd",
            compensation_interval="year",
            source_metadata=metadata,
        )
        listing = JobListing.ingest(source, first)
        self.assertEqual(listing.compensation_currency, "USD")
        self.assertEqual(listing.source_metadata["compensation_basis"], "total")
        self.assertEqual(
            listing.source_metadata["scope"],
            {"countries": ["US", "CA"], "role_families": ["engineering"]},
        )
        replay = JobListing.ingest(source, raw(
            compensation_currency="usd",
            compensation_interval="year",
            source_metadata=metadata,
            first_seen_at=first.first_seen_at,
            last_seen_at=first.last_seen_at,
        ))
        replay.refresh_from_db()
        self.assertEqual(replay.canonical_url, listing.canonical_url)
        self.assertEqual(replay.first_seen_at, listing.first_seen_at)
        self.assertEqual(replay.last_seen_at, listing.last_seen_at)
        self.assertEqual(replay.source_metadata, listing.source_metadata)

    def test_reserved_metadata_keys_are_normalized_or_dropped(self):
        with self.assertRaises(JobSchemaError):
            raw(source_metadata={"response_body": "raw"})
        listing = raw(source_metadata={
            "compensation_basis": " BASE ",
            "scope": {
                "countries": [" us ", "ca", "", 42],
                "role_families": ["Engineering"],
                "unexpected": ["dropped"],
            },
        })
        self.assertEqual(listing.source_metadata["compensation_basis"], "base")
        self.assertEqual(
            listing.source_metadata["scope"],
            {"countries": ["us", "ca"], "role_families": ["Engineering"]},
        )
        dropped = raw(source_metadata={
            "compensation_basis": "gross",
            "scope": "not-a-mapping",
        })
        self.assertNotIn("compensation_basis", dropped.source_metadata)
        self.assertNotIn("scope", dropped.source_metadata)
        empty_scope = raw(source_metadata={"scope": {"countries": []}})
        self.assertNotIn("scope", empty_scope.source_metadata)

    def test_close_absent_closes_only_absent_active_of_source(self):
        source = make_source()
        other = make_source(name="Other jobs")
        seen = JobListing.ingest(source, raw())
        absent = JobListing.ingest(
            source,
            raw(external_id="absent-1", canonical_url="https://jobs.example.test/jobs/absent"),
        )
        terminal = JobListing.ingest(
            source,
            raw(external_id="closed-1", canonical_url="https://jobs.example.test/jobs/closed",
                status=JobListing.Status.CLOSED),
        )
        foreign = JobListing.ingest(other, raw())
        closed = JobListing.all_objects.close_absent(source, {seen.pk})
        self.assertEqual(closed, 1)
        absent.refresh_from_db()
        self.assertEqual(absent.status, JobListing.Status.CLOSED)
        seen.refresh_from_db()
        self.assertEqual(seen.status, JobListing.Status.ACTIVE)
        terminal.refresh_from_db()
        self.assertEqual(terminal.status, JobListing.Status.CLOSED)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, JobListing.Status.ACTIVE)
        # Idempotent on a second call.
        self.assertEqual(JobListing.all_objects.close_absent(source, {seen.pk}), 0)


class ReservedMetadataEdgeTests(TestCase):
    def test_non_mapping_metadata_still_raises_typed_error(self):
        with self.assertRaises(JobSchemaError):
            raw(source_metadata=["not", "a", "mapping"])
