# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The explicit refresh behind the "Evidence changed" notice (issue #473)."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from crank.models.company_profile import CompanyFieldEvidence
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.organization import Organization
from crank.models.preference import UserPreference
from crank.services import match_recompute
from crank.services.match_recompute import RecomputeStatus, recompute_user

URL = "/api/job-matches/refresh/"
RECOMPUTE = "crank.views.job_matches.match_recompute.recompute_user"


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    MATCH_RESULTS_READ_ENABLED=True,
    MATCH_RECOMPUTE_ENABLED=True,
)
class JobMatchRefreshTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.owner = User.objects.create_user("owner", password="secret")
        self.other = User.objects.create_user("other", password="secret")
        self.organization = Organization.objects.create(name="Acme", rto_policy="R")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        for n in range(3):
            JobListing.all_objects.create(
                source=self.source,
                external_id=f"engineer-{n}",
                canonical_url=f"https://jobs.example.test/engineer-{n}",
                employer_name=self.organization.name,
                title=f"Engineer {n}",
                first_seen_at=self.now - timedelta(days=1),
                last_seen_at=self.now,
                status=JobListing.Status.ACTIVE,
                organization=self.organization,
            )
        self.evidence = CompanyFieldEvidence.objects.create(
            organization=self.organization,
            field_key=CompanyFieldEvidence.FieldKey.RTO_POLICY,
            value_text="Remote",
            source_url="https://acme.example/about",
            source_domain="acme.example",
            observed_at=self.now - timedelta(days=2),
            last_verified_at=self.now - timedelta(days=2),
            validation_version="v1",
            extractor_version="v1",
            state=CompanyFieldEvidence.State.ACCEPTED,
        )
        for user in (self.owner, self.other):
            UserPreference.objects.create(
                user=user, revision=0,
                preferences={"work_location": {"modes": ["remote"]}},
            )
            self.assertEqual(
                recompute_user(user, reason="test").status, RecomputeStatus.PUBLISHED
            )
        self.client = Client()
        self.client.force_login(self.owner)

    def _generation(self, user):
        return MatchResultState.objects.get(user=user).current_generation

    def _cited_states(self):
        payload = self.client.get("/api/job-matches/ranked/").json()
        return {
            requirement["evidence_status"]["state"]
            for match in payload["job_matches"]
            for requirement in match["requirements"]
        }

    def test_requires_login(self):
        response = Client().post(URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_rejects_get(self):
        self.assertEqual(self.client.get(URL).status_code, 405)

    def test_rejects_a_post_without_a_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        with mock.patch(RECOMPUTE) as recompute:
            self.assertEqual(client.post(URL).status_code, 403)
        recompute.assert_not_called()

    def test_current_evidence_needs_no_recompute(self):
        before = self._generation(self.owner)
        with mock.patch(RECOMPUTE) as recompute:
            response = self.client.post(URL)
        self.assertEqual(response.json(), {"status": "not_needed"})
        recompute.assert_not_called()
        self.assertEqual(self._generation(self.owner), before)

    def test_deleted_evidence_is_recomputed_for_the_requester_only(self):
        owner_before = self._generation(self.owner)
        other_before = self._generation(self.other)
        other_rows = sorted(JobMatch.objects.filter(user=self.other).values_list("pk", flat=True))
        # A deleted row records no publication event, so nothing else would
        # ever recompute the generation that cites it.
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        self.assertEqual(self._cited_states(), {"missing"})
        self.assertEqual(
            recompute_user(self.owner, reason="drain").status, RecomputeStatus.CURRENT
        )

        response = self.client.post(URL)

        self.assertEqual(response.json(), {"status": "published"})
        self.assertGreater(self._generation(self.owner), owner_before)
        self.assertEqual(self._cited_states(), {"profile"})
        self.assertEqual(self._generation(self.other), other_before)
        self.assertEqual(
            sorted(JobMatch.objects.filter(user=self.other).values_list("pk", flat=True)),
            other_rows,
        )
        # Resolved: a repeat does no work.
        with mock.patch(RECOMPUTE) as recompute:
            self.assertEqual(self.client.post(URL).json(), {"status": "not_needed"})
        recompute.assert_not_called()

    def test_superseded_evidence_is_recomputed(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).update(
            state=CompanyFieldEvidence.State.SUPERSEDED
        )
        self.assertEqual(self._cited_states(), {"superseded"})
        self.assertEqual(self.client.post(URL).json(), {"status": "published"})
        self.assertEqual(self._cited_states(), {"profile"})

    def test_the_check_reads_evidence_once_however_many_matches(self):
        with mock.patch(RECOMPUTE) as recompute:
            with CaptureQueriesContext(connection) as three:
                self.client.post(URL)
            JobMatch.objects.filter(user=self.owner).exclude(
                pk=JobMatch.objects.filter(user=self.owner).first().pk
            ).delete()
            with CaptureQueriesContext(connection) as one:
                self.client.post(URL)
        recompute.assert_not_called()
        self.assertEqual(len(three), len(one))

    def test_no_preferences_needs_no_recompute(self):
        UserPreference.objects.filter(user=self.owner).delete()
        with mock.patch(RECOMPUTE) as recompute:
            self.assertEqual(self.client.post(URL).json(), {"status": "not_needed"})
        recompute.assert_not_called()

    @override_settings(MATCH_RESULTS_READ_ENABLED=False)
    def test_live_reads_need_no_recompute(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        with mock.patch(RECOMPUTE) as recompute:
            self.assertEqual(self.client.post(URL).json(), {"status": "not_needed"})
        recompute.assert_not_called()

    @override_settings(MATCH_RECOMPUTE_ENABLED=False)
    def test_honors_the_recompute_switch(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        self.assertFalse(match_recompute.recompute_enabled())
        with mock.patch(RECOMPUTE) as recompute:
            self.assertEqual(self.client.post(URL).json(), {"status": "disabled"})
        recompute.assert_not_called()

    def test_a_failed_recompute_is_reported_not_raised(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        with mock.patch(
            "crank.services.match_recompute.open_snapshot", side_effect=RuntimeError("boom")
        ):
            response = self.client.post(URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "failed"})
