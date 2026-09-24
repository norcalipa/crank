# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the /api/job-matches/status/ empty-state endpoint."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
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
        # AC-4: explore_companies is offered where openings can't be confirmed.
        self.assertIn("explore_companies", payload["actions"])
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

    @override_settings(MATCH_RESULTS_READ_ENABLED=True)
    def test_committed_generation_count_is_a_single_count_statement(self):
        """Round 3: the polled status endpoint counts the committed
        generation with one ``COUNT`` instead of materializing rows."""
        from crank.models.job_match import MatchResultState

        source = self.make_source(enabled=True)
        listing = self.make_listing(source, "Engineer")
        self.make_preference(
            self.user, preferences={"work_location": {"modes": ["remote"]}}
        )
        MatchResultState.objects.create(
            user=self.user, current_generation=1, issued_generation=1
        )
        now = timezone.now()
        JobMatch.objects.create(
            user=self.user,
            listing=listing,
            organization=self.organization,
            preference_version=1,
            ranker_version="1.0.0",
            score=80,
            factors=[],
            first_matched_at=now,
            last_matched_at=now,
            result_generation=1,
        )
        self.client.force_login(self.user)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "ok")
        counts = [q["sql"] for q in queries if q["sql"].startswith("SELECT COUNT")]
        self.assertTrue(counts)
        self.assertFalse(
            [q for q in queries if "crank_jobmatch" in q["sql"] and "requirements" in q["sql"]]
        )

    def test_no_preferences_counts_zero_without_match_queries(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)

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


# ---------------------------------------------------------------------------
# Issue #476: refresh-with-results, partial coverage, genuine zero-match
# ---------------------------------------------------------------------------


def full_preferences(**overrides):
    """A non-default preference document (the #476 test fixture)."""
    from crank.models.preference import default_preferences

    doc = default_preferences()
    # A document at every default counts as "no preferences"; anchor it with
    # one real requirement so fixtures behave like a configured user.
    doc["compensation"]["minimum_salary"] = 100000
    for section, values in overrides.items():
        if isinstance(values, dict) and isinstance(doc.get(section), dict):
            doc[section].update(values)
        else:
            doc[section] = values
    return doc


class RefreshingWithResultsTests(TestCase):
    """A running crawl must never mask existing results (AC 1/2a)."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("user", password="secret")
        self.organization = Organization.objects.create(name="Acme")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            enabled=True,
        )
        listing = self.make_listing("Active")
        self.make_preference()
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

    def make_listing(self, title, status=JobListing.Status.ACTIVE):
        now = timezone.now()
        return JobListing.all_objects.create(
            source=self.source,
            external_id=title.lower(),
            canonical_url=f"https://jobs.example.test/{title.lower()}",
            employer_name=self.organization.name,
            title=title,
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=status,
            organization=self.organization,
        )

    def make_preference(self):
        from crank.models.preference import UserPreference

        return UserPreference.objects.create(
            user=self.user, preferences=full_preferences(), schema_version=2
        )

    def test_running_crawl_keeps_ok_state_with_refreshing_flag(self):
        from crank.models.crawl_run import CrawlRun

        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            source_key="synthetic.v1",
            outcome=CrawlRun.Outcome.RUNNING,
            started_at=timezone.now(),
        )
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["state"], "ok")
        self.assertTrue(payload["refreshing"])

    def test_no_refreshing_flag_without_running_crawl(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        payload = response.json()
        self.assertEqual(payload["state"], "ok")
        self.assertNotIn("refreshing", payload)

    def test_running_crawl_with_zero_matches_keeps_no_matches(self):
        from crank.models.crawl_run import CrawlRun
        from crank.models.preference import UserPreference

        UserPreference.objects.all().delete()
        JobMatch.objects.all().delete()
        self.make_preference()
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            source_key="synthetic.v1",
            outcome=CrawlRun.Outcome.RUNNING,
            started_at=timezone.now(),
        )
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        payload = response.json()
        self.assertEqual(payload["state"], "no_matches")
        self.assertTrue(payload["refreshing"])

    def test_crawl_running_label_kept_for_first_gather(self):
        """crawl_running still exists for a running crawl with no listings."""
        from crank.models.crawl_run import CrawlRun

        JobMatch.objects.all().delete()
        JobListing.all_objects.all().delete()
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            source_key="synthetic.v1",
            outcome=CrawlRun.Outcome.RUNNING,
            started_at=timezone.now(),
        )
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.json()["state"], "crawl_running")


class PartialCoverageTests(TestCase):
    """Mixed source outcomes yield partial_coverage alongside results (AC 2b/7)."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("user", password="secret")
        self.staff_user = User.objects.create_user(
            "staff", password="secret", is_staff=True
        )
        self.organization = Organization.objects.create(name="Acme")
        self.healthy = JobSourceCatalog.objects.create(
            name="Healthy",
            adapter_key="healthy.v1",
            base_url="https://jobs.example.test",
            enabled=True,
            last_crawl_at=timezone.now(),
        )
        self.degraded = JobSourceCatalog.objects.create(
            name="Degraded",
            adapter_key="degraded.v1",
            base_url="https://jobs.example.test",
            enabled=True,
            last_crawl_at=timezone.now(),
        )
        from crank.models.crawl_run import CrawlRun

        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            job_source=self.healthy,
            source_key="healthy.v1",
            outcome=CrawlRun.Outcome.SUCCESS,
            started_at=timezone.now() - timedelta(hours=1),
            finished_at=timezone.now() - timedelta(hours=1),
        )
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            job_source=self.degraded,
            source_key="degraded.v1",
            outcome=CrawlRun.Outcome.FAILURE,
            started_at=timezone.now() - timedelta(hours=1),
            finished_at=timezone.now() - timedelta(hours=1),
        )
        listing = JobListing.all_objects.create(
            source=self.healthy,
            external_id="active",
            canonical_url="https://jobs.example.test/active",
            employer_name=self.organization.name,
            title="Active",
            first_seen_at=timezone.now() - timedelta(days=1),
            last_seen_at=timezone.now(),
            status=JobListing.Status.ACTIVE,
            organization=self.organization,
        )
        from crank.models.preference import UserPreference

        UserPreference.objects.create(
            user=self.user, preferences=full_preferences(), schema_version=2
        )
        UserPreference.objects.create(
            user=self.staff_user, preferences=full_preferences(), schema_version=2
        )
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
        # The staff user needs their own match so they land in the same
        # partial_coverage branch when asserting staff-only detail stripping.
        JobMatch.objects.create(
            user=self.staff_user,
            listing=listing,
            organization=self.organization,
            preference_version=1,
            ranker_version="1.0.0",
            score=80,
            factors=[],
            first_matched_at=timezone.now(),
            last_matched_at=timezone.now(),
        )

    def test_partial_coverage_state_with_results(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["state"], "partial_coverage")
        self.assertEqual(
            payload["coverage"],
            {"enabled_sources": 2, "failing_sources": 1},
        )
        self.assertEqual(payload["inventory"]["active_listings"], 1)

    def test_partial_coverage_staff_detail_only_for_staff(self):
        self.client.force_login(self.user)
        payload = self.client.get("/api/job-matches/status/").json()
        self.assertNotIn("staff_detail", payload)
        # Coverage numbers themselves are user-safe and stay.
        self.assertIn("coverage", payload)

        self.client.force_login(self.staff_user)
        staff_payload = self.client.get("/api/job-matches/status/").json()
        self.assertIn("staff_detail", staff_payload)
        self.assertIn("degraded", staff_payload["staff_detail"])

    def test_all_healthy_sources_keep_ok(self):
        from crank.models.crawl_run import CrawlRun

        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            job_source=self.degraded,
            source_key="degraded.v1",
            outcome=CrawlRun.Outcome.SUCCESS,
            started_at=timezone.now() - timedelta(hours=1),
            finished_at=timezone.now() - timedelta(hours=1),
        )
        self.client.force_login(self.user)
        payload = self.client.get("/api/job-matches/status/").json()
        self.assertEqual(payload["state"], "ok")

    def test_single_failing_source_is_not_partial(self):
        """One degraded source with no healthy source is not partial."""
        self.healthy.enabled = False
        self.healthy.save()
        self.client.force_login(self.user)
        payload = self.client.get("/api/job-matches/status/").json()
        self.assertNotEqual(payload["state"], "partial_coverage")

    def test_transition_ok_to_partial_and_back(self):
        """ok -> partial when a source degrades; back when it recovers."""
        from crank.models.crawl_run import CrawlRun

        self.client.force_login(self.user)
        # Recover the degraded source first so the starting state is ok.
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            job_source=self.degraded,
            source_key="degraded.v1",
            outcome=CrawlRun.Outcome.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
        )
        self.assertEqual(
            self.client.get("/api/job-matches/status/").json()["state"], "ok"
        )
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            job_source=self.degraded,
            source_key="degraded.v1",
            outcome=CrawlRun.Outcome.TIMEOUT,
            started_at=timezone.now(),
            finished_at=timezone.now(),
        )
        self.assertEqual(
            self.client.get("/api/job-matches/status/").json()["state"],
            "partial_coverage",
        )
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            job_source=self.degraded,
            source_key="degraded.v1",
            outcome=CrawlRun.Outcome.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
        )
        self.assertEqual(
            self.client.get("/api/job-matches/status/").json()["state"], "ok"
        )


class NoMatchesExplanationTests(TestCase):
    """Genuine zero-match carries constraints, inventory facts, preview (AC 2c/3)."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("user", password="secret")
        self.organization = Organization.objects.create(name="Acme")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            enabled=True,
        )
        for title in ("Alpha", "Beta", "Gamma"):
            self.make_listing(title)
        from crank.models.preference import UserPreference

        UserPreference.objects.create(
            user=self.user,
            preferences=full_preferences(
                exclusions={"companies": ["acme"]},
                compensation={"minimum_salary": 150000},
            ),
            schema_version=2,
        )

    def make_listing(self, title, status=JobListing.Status.ACTIVE):
        now = timezone.now()
        return JobListing.all_objects.create(
            source=self.source,
            external_id=title.lower(),
            canonical_url=f"https://jobs.example.test/{title.lower()}",
            employer_name=self.organization.name,
            title=title,
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=status,
            organization=self.organization,
        )

    def test_no_matches_carries_constraints_and_inventory(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/status/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["state"], "no_matches")
        self.assertIn("Excluded companies: acme", payload["active_constraints"])
        self.assertIn("Minimum salary 150,000", payload["active_constraints"])
        self.assertEqual(payload["inventory"]["active_listings"], 3)
        self.assertIn("explore_companies", payload["actions"])

    def test_no_matches_relaxation_preview(self):
        self.client.force_login(self.user)
        payload = self.client.get("/api/job-matches/status/").json()
        preview = payload["relaxation_preview"]
        self.assertIsNotNone(preview)
        self.assertEqual(preview["field"], "exclusions")
        # Round-1 visual critique: the preview names the concrete dimension.
        self.assertEqual(
            preview["label"], "Removing excluded companies (currently acme)"
        )
        self.assertEqual(preview["added_count"], 3)

    def test_preview_disabled_by_setting(self):
        self.client.force_login(self.user)
        with override_settings(JOB_MATCH_RELAXATION_PROBES=0):
            payload = self.client.get("/api/job-matches/status/").json()
        self.assertIsNone(payload["relaxation_preview"])

    def test_status_never_writes_preferences(self):
        from crank.models.preference import UserPreference, UserPreferenceAudit

        self.client.force_login(self.user)
        before = UserPreference.objects.get(user=self.user)
        before_doc = dict(before.preferences)
        for _ in range(3):
            self.client.get("/api/job-matches/status/")
        after = UserPreference.objects.get(user=self.user)
        self.assertEqual(after.preferences, before_doc)
        self.assertEqual(UserPreferenceAudit.objects.count(), 0)

    def test_no_matches_copy_rules(self):
        """User-facing copy has no class names or operator instructions (AC 6)."""
        self.client.force_login(self.user)
        payload = self.client.get("/api/job-matches/status/").json()
        rendered = " ".join(
            [payload["title"], payload["message"]] + payload.get("active_constraints", [])
        )
        for banned in ("UserPreference", "JobSourceCatalog", "CrawlRun", "enabled=True", "JobListing"):
            self.assertNotIn(banned, rendered)


class StateTransitionTests(TestCase):
    """Empty-inventory to populated transitions (test plan section 6)."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("user", password="secret")
        self.organization = Organization.objects.create(name="Acme")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            enabled=True,
            last_crawl_at=timezone.now(),
        )
        from crank.models.preference import UserPreference

        UserPreference.objects.create(
            user=self.user, preferences=full_preferences(), schema_version=2
        )

    def make_listing(self, title, status=JobListing.Status.ACTIVE):
        now = timezone.now()
        return JobListing.all_objects.create(
            source=self.source,
            external_id=title.lower(),
            canonical_url=f"https://jobs.example.test/{title.lower()}",
            employer_name=self.organization.name,
            title=title,
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=status,
            organization=self.organization,
        )

    def make_match(self, listing):
        return JobMatch.objects.create(
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

    def test_crawl_empty_to_ok_transition(self):
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get("/api/job-matches/status/").json()["state"], "crawl_empty"
        )
        self.make_match(self.make_listing("Active"))
        payload = self.client.get("/api/job-matches/status/").json()
        self.assertEqual(payload["state"], "ok")

    def test_ok_to_partial_transition_on_source_degradation(self):
        degraded = JobSourceCatalog.objects.create(
            name="Degraded",
            adapter_key="degraded.v1",
            base_url="https://jobs.example.test",
            enabled=True,
            last_crawl_at=timezone.now(),
        )
        self.client.force_login(self.user)
        # Results first: ok with one healthy source carrying listings.
        self.make_match(self.make_listing("Active"))
        self.assertEqual(
            self.client.get("/api/job-matches/status/").json()["state"], "ok"
        )
        # Only then does the second source degrade — with results present the
        # failure becomes a coverage notice instead of replacing the panel.
        from crank.models.crawl_run import CrawlRun

        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            job_source=degraded,
            source_key="degraded.v1",
            outcome=CrawlRun.Outcome.FAILURE,
            started_at=timezone.now(),
            finished_at=timezone.now(),
        )
        payload = self.client.get("/api/job-matches/status/").json()
        self.assertEqual(payload["state"], "partial_coverage")
