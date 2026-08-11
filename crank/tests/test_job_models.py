# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from datetime import timedelta
from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from crank.admin import JobListingAdmin, JobSourceCatalogAdmin
from crank.agents.jobs.base import RawJobListing
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
        from crank.agents.jobs.base import JobSourceQuery

        with self.assertRaises(Exception):
            JobSourceQuery(max_listings=0)
        with self.assertRaises(Exception):
            JobSourceQuery(max_pages=1001)


class JobListingModelTests(TestCase):
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


class JobAdminAuthorizationTests(TestCase):
    def test_job_admin_is_staff_only_and_listing_content_is_not_in_list(self):
        site = AdminSite()
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, site)
        listing_admin = JobListingAdmin(JobListing, site)
        request = type("Request", (), {"user": User.objects.create_user(username="u")})()
        self.assertFalse(source_admin.has_view_permission(request))
        self.assertFalse(listing_admin.has_view_permission(request))
        self.assertNotIn("description_excerpt", listing_admin.list_display)
