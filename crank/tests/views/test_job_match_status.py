# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the /api/job-matches/status/ empty-state endpoint."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.organization import Organization


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class JobMatchStatusViewTests(TestCase):
    """Tests for the /api/job-matches/status/ empty-state endpoint."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("user", password="secret")
        self.staff_user = User.objects.create_user(
            "staff", password="secret", is_staff=True
        )
        self.organization = Organization.objects.create(name="Acme")

    def make_source(self, *, enabled=False, name="Synthetic"):
        return JobSourceCatalog.objects.create(
            name=name,
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            enabled=enabled,
        )

    def make_listing(self, source, title, status=JobListing.Status.ACTIVE):
        now = timezone.now()
        return JobListing.all_objects.create(
            source=source,
            external_id=title.lower(),
            canonical_url=f"https://jobs.example.test/{title.lower()}",
            employer_name=self.organization.name,
            title=title,
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=status,
            organization=self.organization,
        )

    def make_preference(self, user, *, preferences=None):
        from crank.models.preference import UserPreference
        pref = UserPreference.objects.create(user=user)
        if preferences is not None:
            pref.preferences = preferences
            pref.save(update_fields=["preferences", "modified"])
        return pref

    def test_anonymous_requests_are_rejected(self):
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 302)

    def test_no_source_state(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["state"], "no_source")
        self.assertIn("suggest_company", payload["actions"])
        self.assertNotIn("staff_detail", payload)

    def test_source_disabled_state(self):
        self.make_source(enabled=False)
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "source_disabled")

    def test_crawl_empty_state(self):
        source = self.make_source(enabled=True)
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "crawl_empty")

    def test_crawl_stale_state(self):
        source = self.make_source(enabled=True)
        self.make_listing(source, "Old", status=JobListing.Status.EXPIRED)
        self.make_listing(source, "Closed", status=JobListing.Status.CLOSED)
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "crawl_stale")

    def test_no_preferences_state(self):
        source = self.make_source(enabled=True)
        self.make_listing(source, "Active")
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["state"], "no_preferences")
        self.assertIn("chat", payload["actions"])

    def test_no_matches_state(self):
        source = self.make_source(enabled=True)
        self.make_listing(source, "Active")
        prefs = {
            "compensation": {"minimum_salary": 100000, "currency": "USD", "equity_minimum_percent": None},
            "culture": [], "work_location": {"modes": [], "countries": [], "require_onsite": None},
            "geography": {"regions": [], "remote_friendly": None},
            "industry": [], "funding_stage": [],
            "vesting": {"max_cliff_months": None, "max_vesting_months": None, "prefer_accelerated": None},
            "exclusions": {"companies": [], "titles": [], "industries": [], "locations": []},
            "priorities": {}, "notes": "",
        }
        self.make_preference(self.user, preferences=prefs)
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["state"], "no_matches")
        self.assertIn("chat", payload["actions"])

    def test_ok_state_with_matches(self):
        source = self.make_source(enabled=True)
        listing = self.make_listing(source, "Active")
        prefs = {
            "compensation": {"minimum_salary": 100000, "currency": "USD", "equity_minimum_percent": None},
            "culture": [], "work_location": {"modes": [], "countries": [], "require_onsite": None},
            "geography": {"regions": [], "remote_friendly": None},
            "industry": [], "funding_stage": [],
            "vesting": {"max_cliff_months": None, "max_vesting_months": None, "prefer_accelerated": None},
            "exclusions": {"companies": [], "titles": [], "industries": [], "locations": []},
            "priorities": {}, "notes": "",
        }
        self.make_preference(self.user, preferences=prefs)
        JobMatch.objects.create(
            user=self.user,
            listing=listing,
            organization=self.organization,
            preference_version=1,
            ranker_version="1.0.0",
            score=80,
            factors=[],
            first_matched_at=timezone.now(),
            last_matched_at=timezone.now(),
        )
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["state"], "ok")
        self.assertEqual(payload["actions"], [])

    def test_staff_detail_included_for_staff(self):
        self.make_source(enabled=False)
        self.client.force_login(self.staff_user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["state"], "source_disabled")
        self.assertIn("staff_detail", payload)

    def test_staff_detail_excluded_for_non_staff(self):
        self.make_source(enabled=False)
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("staff_detail", payload)

    def test_crawl_running_state(self):
        from crank.models.crawl_run import CrawlRun
        self.make_source(enabled=True)
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            source_key="synthetic.v1",
            outcome=CrawlRun.Outcome.RUNNING,
            started_at=timezone.now(),
        )
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "crawl_running")

    def test_crawl_failed_state(self):
        from crank.models.crawl_run import CrawlRun
        self.make_source(enabled=True)
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            source_key="synthetic.v1",
            outcome=CrawlRun.Outcome.FAILURE,
            started_at=timezone.now() - timedelta(hours=2),
            finished_at=timezone.now() - timedelta(hours=1),
        )
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "crawl_failed")

    def test_crawl_failed_not_shown_if_old(self):
        from crank.models.crawl_run import CrawlRun
        self.make_source(enabled=True)
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            source_key="synthetic.v1",
            outcome=CrawlRun.Outcome.FAILURE,
            started_at=timezone.now() - timedelta(days=3),
            finished_at=timezone.now() - timedelta(days=2, hours=23),
        )
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["state"], "crawl_failed")

    def test_response_shape(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("state", payload)
        self.assertIn("title", payload)
        self.assertIn("message", payload)
        self.assertIn("actions", payload)
        self.assertIsInstance(payload["actions"], list)
