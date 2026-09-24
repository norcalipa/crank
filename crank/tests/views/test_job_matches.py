# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.organization import Organization
from crank.models.preference import UserPreference


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class JobMatchViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user("owner", password="secret")
        self.other = User.objects.create_user("other", password="secret")
        self.organization = Organization.objects.create(name="Acme")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
        )
        self.active = self.make_listing("Active", JobListing.Status.ACTIVE)
        self.closed = self.make_listing("Closed", JobListing.Status.CLOSED)
        self.expired = self.make_listing("Expired", JobListing.Status.EXPIRED)
        self.now = timezone.now()
        # issue #475 review: reads hide stored matches for a user with no
        # UserPreference row, so every owner-read test needs one.
        self.owner_pref = UserPreference.objects.create(user=self.owner, revision=0)

    def make_listing(self, title, status):
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

    def make_match(self, user, listing, *, score=50, dismissed=False):
        return JobMatch.objects.create(
            user=user,
            listing=listing,
            organization=listing.organization,
            preference_version=1,
            ranker_version="1.0.0",
            score=score,
            factors=[{"factor": "culture", "score": score, "detail": "team fit"}],
            first_matched_at=self.now,
            last_matched_at=self.now,
            dismissed=dismissed,
        )

    def test_anonymous_requests_are_rejected(self):
        for url in (
            "/api/job-matches/",
            "/api/job-matches/1/",
            "/api/job-matches/1/seen/",
            "/api/job-matches/1/dismiss/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code if url.endswith("/") and not url.endswith(("seen/", "dismiss/")) else self.client.post(url).status_code, 302)

    def test_list_is_owner_scoped_sorted_paginated_and_hides_inactive(self):
        first = self.make_match(self.owner, self.active, score=10)
        second_listing = self.make_listing("Second", JobListing.Status.ACTIVE)
        second = self.make_match(self.owner, second_listing, score=90)
        self.make_match(self.owner, self.closed, score=100)
        self.make_match(self.owner, self.expired, score=99)
        dismissed_listing = self.make_listing("Dismissed", JobListing.Status.ACTIVE)
        self.make_match(self.owner, dismissed_listing, score=80, dismissed=True)
        self.make_match(self.other, second_listing, score=100)

        self.client.force_login(self.owner)
        response = self.client.get("/api/job-matches/?page=1&page_size=1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["results"][0]["id"], second.pk)
        self.assertIsNotNone(payload["next"])
        self.assertEqual(response.json()["results"][0]["listing"]["status"], "active")

        response = self.client.get("/api/job-matches/?page=2&page_size=1")
        self.assertEqual(response.json()["results"][0]["id"], first.pk)

    def test_invalid_page_parameters_return_bad_request(self):
        self.client.force_login(self.owner)
        for query in ("page=zero", "page=-1", "page_size=0"):
            with self.subTest(query=query):
                response = self.client.get(f"/api/job-matches/?{query}")
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())

    def test_detail_includes_factors_and_prevents_id_guessing(self):
        match = self.make_match(self.owner, self.active)
        other_match = self.make_match(self.other, self.active, score=90)
        self.client.force_login(self.owner)

        response = self.client.get(f"/api/job-matches/{match.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["factors"][0]["factor"], "culture")
        match.organization = None
        match.save(update_fields=["organization", "modified"])
        self.assertIsNone(self.client.get(f"/api/job-matches/{match.pk}/").json()["organization"])
        self.assertEqual(self.client.get(f"/api/job-matches/{other_match.pk}/").status_code, 404)
        self.assertEqual(self.client.get("/api/job-matches/999999/").status_code, 404)

    def test_seen_marks_once_and_dismiss_hides_match(self):
        match = self.make_match(self.owner, self.active)
        self.client.force_login(self.owner)

        response = self.client.post(f"/api/job-matches/{match.pk}/seen/")
        self.assertEqual(response.status_code, 200)
        seen_at = response.json()["seen_at"]
        self.assertIsNotNone(seen_at)
        self.assertEqual(self.client.post(f"/api/job-matches/{match.pk}/seen/").json()["seen_at"], seen_at)

        response = self.client.post(f"/api/job-matches/{match.pk}/dismiss/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dismissed"])
        self.assertEqual(self.client.get("/api/job-matches/").json()["count"], 0)

    def test_seen_and_dismiss_404_without_preference_and_do_not_mutate(self):
        match = self.make_match(self.owner, self.active)
        self.client.force_login(self.owner)
        UserPreference.objects.filter(user=self.owner).delete()
        for action in ("seen", "dismiss"):
            with self.subTest(action=action):
                response = self.client.post(f"/api/job-matches/{match.pk}/{action}/")
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(b"score", response.content)
                match.refresh_from_db()
                self.assertIsNone(match.seen_at)
                self.assertFalse(match.dismissed)

    def test_seen_repairs_a_disagreeing_version_row_even_when_requested_row_is_already_seen(self):
        """Posting an already-seen version row must still repair a sibling
        version row for the same listing that disagrees (issue #475 review
        round 2, MINOR finding 3): gating the all-version update on the
        requested row's own state skipped exactly this repair."""
        seen_row = self.make_match(self.owner, self.active)
        seen_row.seen_at = self.now
        seen_row.save(update_fields=["seen_at", "modified"])
        unseen_row = JobMatch.objects.create(
            user=self.owner,
            listing=self.active,
            organization=self.active.organization,
            preference_version=2,
            ranker_version="1.0.0",
            score=50,
            factors=[],
            first_matched_at=self.now,
            last_matched_at=self.now,
        )
        self.assertIsNone(unseen_row.seen_at)
        self.client.force_login(self.owner)

        response = self.client.post(f"/api/job-matches/{seen_row.pk}/seen/")

        self.assertEqual(response.status_code, 200)
        unseen_row.refresh_from_db()
        self.assertIsNotNone(unseen_row.seen_at)

    def test_dismiss_repairs_a_disagreeing_version_row_even_when_requested_row_is_already_dismissed(self):
        dismissed_row = self.make_match(self.owner, self.active, dismissed=True)
        undismissed_row = JobMatch.objects.create(
            user=self.owner,
            listing=self.active,
            organization=self.active.organization,
            preference_version=2,
            ranker_version="1.0.0",
            score=50,
            factors=[],
            first_matched_at=self.now,
            last_matched_at=self.now,
            dismissed=False,
        )
        self.client.force_login(self.owner)

        response = self.client.post(f"/api/job-matches/{dismissed_row.pk}/dismiss/")

        self.assertEqual(response.status_code, 200)
        undismissed_row.refresh_from_db()
        self.assertTrue(undismissed_row.dismissed)

    def test_actions_are_owner_scoped_and_inactive_matches_are_not_mutable(self):
        other_match = self.make_match(self.other, self.active)
        closed_match = self.make_match(self.owner, self.closed)
        self.client.force_login(self.owner)

        for suffix in ("seen/", "dismiss/"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.client.post(f"/api/job-matches/{other_match.pk}/{suffix}").status_code, 404)
                self.assertEqual(self.client.post(f"/api/job-matches/{closed_match.pk}/{suffix}").status_code, 404)

    def test_stale_result_is_flagged(self):
        """A result computed from an older preference revision is flagged stale."""
        from crank.models.preference import default_preferences
        self.owner_pref.revision = 5
        self.owner_pref.preferences = default_preferences()
        self.owner_pref.save(update_fields=["revision", "preferences", "modified"])
        match = self.make_match(self.owner, self.active)
        match.preference_revision = 3
        match.requirements = [
            {"path": "compensation.minimum_salary", "status": "match", "observed": 150000,
             "source_kind": "field", "source_id": "listing.compensation_min", "scope_ok": True}
        ]
        match.evidence_ids = []
        match.data_revision = None
        match.generated_at = self.now
        match.save()
        self.client.force_login(self.owner)
        payload = self.client.get(f"/api/job-matches/{match.pk}/").json()
        self.assertTrue(payload["revision"]["stale"])
        self.assertEqual(payload["revision"]["preference_revision"], 3)
        self.assertEqual(payload["revision"]["ranking_version"], "1.0.0")
        self.assertEqual(payload["fit_score"], match.score)
        self.assertEqual(payload["requirements"][0]["path"], "compensation.minimum_salary")

    def test_pre_migration_row_serializes_without_error(self):
        """A row with NULL revisions falls back to the un-revised shape."""
        match = self.make_match(self.owner, self.active)
        self.assertIsNone(match.preference_revision)
        self.client.force_login(self.owner)
        payload = self.client.get(f"/api/job-matches/{match.pk}/").json()
        self.assertFalse(payload["revision"]["stale"])
        self.assertIsNone(payload["revision"]["preference_revision"])
        self.assertIsNone(payload["revision"]["generated_at"])

    def test_revision_block_gains_additive_generation_fields(self):
        match = self.make_match(self.owner, self.active)
        self.client.force_login(self.owner)
        payload = self.client.get(f"/api/job-matches/{match.pk}/").json()
        self.assertIn("result_generation", payload["revision"])
        self.assertIn("pending", payload["revision"])
        self.assertIsNone(payload["revision"]["result_generation"])
        self.assertFalse(payload["revision"]["pending"])

    def test_deleted_preference_hides_stored_matches_in_every_read(self):
        """issue #475 review finding 4 / owner decision: deleting the
        UserPreference must hide stored matches in list, detail, and the
        status count, without physically deleting the JobMatch rows."""
        match = self.make_match(self.owner, self.active)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/api/job-matches/").json()["count"], 1)
        self.assertEqual(self.client.get(f"/api/job-matches/{match.pk}/").status_code, 200)

        self.owner_pref.delete()

        self.assertEqual(self.client.get("/api/job-matches/").json()["count"], 0)
        self.assertEqual(self.client.get(f"/api/job-matches/{match.pk}/").status_code, 404)
        # The status endpoint must not 500 or leak the match count.
        self.assertEqual(self.client.get("/api/job-matches/status/").status_code, 200)
        # No physical deletion of the stored match.
        self.assertTrue(JobMatch.objects.filter(pk=match.pk).exists())


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    MATCH_RESULTS_READ_ENABLED=True,
)
class CommittedGenerationReadGateTests(TestCase):
    """issue #475 AC4: list/status and the ranked endpoint read the same
    committed generation once one has been published, with an identical
    revision block; with no generation yet, both fall back to today's live
    behavior unchanged."""

    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user("owner", password="secret")
        self.organization = Organization.objects.create(name="Acme")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        now = timezone.now()
        self.listing = JobListing.all_objects.create(
            source=self.source,
            external_id="engineer",
            canonical_url="https://jobs.example.test/engineer",
            employer_name=self.organization.name,
            title="Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=self.organization,
        )
        self.pref = UserPreference.objects.create(
            user=self.owner,
            revision=0,
            preferences={"work_location": {"modes": ["remote"]}},
        )

    def test_fallback_to_live_with_no_generation_yet(self):
        JobMatch.objects.create(
            user=self.owner,
            listing=self.listing,
            organization=self.organization,
            preference_version=1,
            ranker_version="1.0.0",
            score=50,
            first_matched_at=timezone.now(),
            last_matched_at=timezone.now(),
        )
        self.client.force_login(self.owner)
        payload = self.client.get("/api/job-matches/").json()
        self.assertEqual(payload["count"], 1)

    def test_list_and_status_use_the_committed_generation(self):
        from crank.services.match_recompute import recompute_user

        outcome = recompute_user(self.owner, reason="preference")
        self.client.force_login(self.owner)

        list_payload = self.client.get("/api/job-matches/").json()
        self.assertEqual(list_payload["count"], 1)
        self.assertEqual(
            list_payload["results"][0]["revision"]["result_generation"],
            outcome.generation,
        )

        status_payload = self.client.get("/api/job-matches/status/").json()
        self.assertEqual(status_payload["state"], "ok")

        ranked_payload = self.client.get("/api/job-matches/ranked/").json()
        self.assertEqual(len(ranked_payload["job_matches"]), 1)
        self.assertEqual(
            ranked_payload["job_matches"][0]["revision"]["result_generation"],
            outcome.generation,
        )
        self.assertEqual(
            ranked_payload["job_matches"][0]["revision"],
            list_payload["results"][0]["revision"],
        )

    def test_assistant_and_ranked_endpoint_share_the_same_generation(self):
        from crank.agents.job_search.tools import get_matches_for_user
        from crank.services.match_recompute import recompute_user

        outcome = recompute_user(self.owner, reason="preference")
        self.client.force_login(self.owner)

        ranked_payload = self.client.get("/api/job-matches/ranked/").json()
        assistant_payload = get_matches_for_user(self.owner)

        self.assertEqual(
            [m["listing_id"] for m in ranked_payload["job_matches"]],
            [m["listing_id"] for m in assistant_payload["job_matches"]],
        )
        self.assertEqual(
            ranked_payload["job_matches"][0]["revision"],
            assistant_payload["job_matches"][0]["revision"],
        )
        self.assertEqual(
            assistant_payload["job_matches"][0]["revision"]["result_generation"],
            outcome.generation,
        )

    def test_dismissed_row_is_hidden_from_committed_reads(self):
        from crank.services.match_recompute import recompute_user

        recompute_user(self.owner, reason="preference")
        match = JobMatch.objects.get()
        match.dismissed = True
        match.save(update_fields=["dismissed", "modified"])
        self.client.force_login(self.owner)
        payload = self.client.get("/api/job-matches/").json()
        self.assertEqual(payload["count"], 0)

    @override_settings(MATCH_RESULTS_READ_ENABLED=False)
    def test_gate_off_falls_back_to_live_even_with_a_generation(self):
        from crank.services.match_recompute import recompute_user

        recompute_user(self.owner, reason="preference")
        self.client.force_login(self.owner)
        payload = self.client.get("/api/job-matches/").json()
        self.assertEqual(payload["count"], 1)

    def test_deleted_preference_hides_committed_generation_from_every_read(self):
        """issue #475 review finding 4 / owner decision: deleting the
        preference hides stored matches even when a committed generation
        exists, and the status endpoint reports the no-preferences state."""
        from crank.services.match_recompute import recompute_user

        recompute_user(self.owner, reason="preference")
        match = JobMatch.objects.get()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/api/job-matches/").json()["count"], 1)

        self.pref.delete()

        self.assertEqual(self.client.get("/api/job-matches/").json()["count"], 0)
        self.assertEqual(self.client.get(f"/api/job-matches/{match.pk}/").status_code, 404)
        status_payload = self.client.get("/api/job-matches/status/").json()
        self.assertEqual(status_payload["state"], "no_preferences")
        self.assertTrue(JobMatch.objects.filter(pk=match.pk).exists())
