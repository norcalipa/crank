# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from datetime import timedelta

from django.contrib.auth.models import User
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from crank.agents.jobs.match_persist import persist_matches
from crank.agents.jobs.matching import JobCriteria, MatchResult
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.organization import Organization


def make_listing(source, organization, *, title="Engineer", status=JobListing.Status.ACTIVE):
    now = timezone.now()
    return JobListing.all_objects.create(
        source=source,
        external_id=f"{title.lower()}-{status}",
        canonical_url=f"https://jobs.example.test/{title.lower()}-{status}",
        employer_name=organization.name,
        title=title,
        first_seen_at=now - timedelta(days=1),
        last_seen_at=now,
        status=status,
        organization=organization,
    )


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class MatchPersistenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", password="secret")
        self.organization = Organization.objects.create(name="Acme")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
        )
        self.criteria = JobCriteria(criteria_version=7)
        self.listing = make_listing(self.source, self.organization)

    def test_upserts_idempotently_and_updates_freshness_and_factors(self):
        before = timezone.now()
        self.assertEqual(
            persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG), 1
        )
        match = JobMatch.objects.get()
        self.assertEqual(str(match), f"{self.listing} for {self.user}")
        self.assertGreaterEqual(match.first_matched_at, before)
        first_matched_at = match.first_matched_at
        first_factors = match.factors

        updated_criteria = JobCriteria(
            work_modes=frozenset({"remote"}), criteria_version=7
        )
        self.listing.is_remote = True
        self.listing.save(update_fields=["is_remote", "modified"])
        self.assertEqual(
            persist_matches(self.user, [self.listing], updated_criteria, DEFAULT_CONFIG), 1
        )
        match.refresh_from_db()
        self.assertEqual(JobMatch.objects.count(), 1)
        self.assertEqual(match.first_matched_at, first_matched_at)
        self.assertGreaterEqual(match.last_matched_at, first_matched_at)
        self.assertNotEqual(match.factors, first_factors)

    def test_unique_version_constraint_and_string_representation(self):
        persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG)
        match = JobMatch.objects.get()
        self.assertEqual(str(match), f"{self.listing} for {self.user}")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JobMatch.objects.create(
                    user=self.user,
                    listing=self.listing,
                    organization=self.organization,
                    preference_version=self.criteria.criteria_version,
                    ranker_version=DEFAULT_CONFIG.version,
                    score=1,
                    first_matched_at=timezone.now(),
                    last_matched_at=timezone.now(),
                )

    def test_skips_excluded_and_closed_or_expired_listings(self):
        excluded = make_listing(self.source, self.organization, title="Excluded")
        closed = make_listing(
            self.source, self.organization, title="Closed", status=JobListing.Status.CLOSED
        )
        expired = make_listing(
            self.source, self.organization, title="Expired", status=JobListing.Status.EXPIRED
        )
        criteria = JobCriteria(excluded_titles=frozenset({"excluded"}), criteria_version=7)

        self.assertEqual(
            persist_matches(
                self.user,
                [self.listing, excluded, closed, expired],
                criteria,
                DEFAULT_CONFIG,
            ),
            1,
        )
        self.assertEqual(JobMatch.objects.values_list("listing_id", flat=True).get(), self.listing.pk)

    @patch("crank.agents.jobs.match_persist.rank_listings")
    def test_ignores_rank_results_without_a_supplied_listing(self, rank_listings):
        rank_listings.return_value = [
            MatchResult(
                listing_id=999999,
                score=50.0,
                excluded=False,
                exclusion_reasons=[],
                factors=[],
                ranker_version=DEFAULT_CONFIG.version,
                criteria_version=self.criteria.criteria_version,
            )
        ]
        self.assertEqual(
            persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG), 0
        )
        self.assertFalse(JobMatch.objects.exists())

    def test_dismissed_match_stays_dismissed_when_refreshed(self):
        persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG)
        match = JobMatch.objects.get()
        match.dismissed = True
        match.save(update_fields=["dismissed", "modified"])

        persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG)
        match.refresh_from_db()
        self.assertTrue(match.dismissed)
