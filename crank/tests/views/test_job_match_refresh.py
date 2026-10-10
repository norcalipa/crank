# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The explicit refresh behind the "Evidence changed" notice (issue #473)."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
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
        cache.clear()
        self.addCleanup(cache.clear)
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

    def test_reaccepting_the_same_value_is_not_a_change(self):
        """Acceptance replaces the row with an equal one; the fact is unchanged."""
        State = CompanyFieldEvidence.State
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).update(state=State.SUPERSEDED)
        CompanyFieldEvidence.objects.create(
            organization=self.organization,
            field_key=CompanyFieldEvidence.FieldKey.RTO_POLICY,
            value_text="Remote",
            source_url="https://acme.example/about",
            source_domain="acme.example",
            observed_at=self.now,
            last_verified_at=self.now,
            validation_version="v1",
            extractor_version="v1",
            state=State.ACCEPTED,
        )
        self.assertNotIn("superseded", self._cited_states())
        with mock.patch(RECOMPUTE) as recompute:
            self.assertEqual(self.client.post(URL).json(), {"status": "not_needed"})
        recompute.assert_not_called()

    def test_replacing_the_value_is_still_a_change(self):
        State = CompanyFieldEvidence.State
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).update(state=State.SUPERSEDED)
        CompanyFieldEvidence.objects.create(
            organization=self.organization,
            field_key=CompanyFieldEvidence.FieldKey.RTO_POLICY,
            value_text="Hybrid",
            source_url="https://acme.example/about",
            source_domain="acme.example",
            observed_at=self.now,
            last_verified_at=self.now,
            validation_version="v1",
            extractor_version="v1",
            state=State.ACCEPTED,
        )
        self.assertEqual(self._cited_states(), {"superseded"})
        self.assertEqual(self.client.post(URL).json(), {"status": "published"})

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

    def test_a_second_recompute_inside_the_cooldown_is_refused(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        with mock.patch(
            "crank.services.match_recompute.open_snapshot", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(self.client.post(URL).json(), {"status": "failed"})
        before = self._generation(self.owner)
        # Still unresolved, but the window has not passed: no second run.
        with mock.patch(RECOMPUTE) as recompute:
            response = self.client.post(URL)
        recompute.assert_not_called()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"status": "rate_limited", "retry_after": 30})
        self.assertEqual(response["Retry-After"], "30")
        self.assertEqual(self._generation(self.owner), before)
        # The window is per user and it ends.
        cache.delete(f"job-match-refresh:{self.owner.pk}")
        self.assertEqual(self.client.post(URL).json(), {"status": "published"})

    def test_retry_after_is_what_is_left_of_the_window(self):
        import time

        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        with mock.patch(RECOMPUTE, return_value=mock.Mock(status=RecomputeStatus.FAILED)):
            self.assertEqual(self.client.post(URL).json(), {"status": "failed"})
            started = time.time()
            with mock.patch("crank.views.job_matches.time.time", return_value=started + 12.4):
                response = self.client.post(URL)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"status": "rate_limited", "retry_after": 18})
        self.assertEqual(response["Retry-After"], "18")

    def test_retry_after_stays_within_the_window_for_any_cached_value(self):
        from crank.views.job_matches import _seconds_left

        import time

        now = time.time()
        # A value from an older release or a clock that moved: never 0,
        # never beyond the window.
        self.assertEqual(_seconds_left(True, 30), 30)
        self.assertEqual(_seconds_left("soon", 30), 30)
        self.assertEqual(_seconds_left(1, 30), 1)
        self.assertEqual(_seconds_left(now - 5, 30), 1)
        self.assertEqual(_seconds_left(now + 600, 30), 30)
        self.assertEqual(_seconds_left(now + 7.2, 30), 8)

    def test_an_entry_that_expired_before_the_read_is_not_the_whole_window(self):
        from crank.views.job_matches import _seconds_left

        # Gone between the refused add and the read: the window is over.
        self.assertEqual(_seconds_left(None, 30), 1)
        self.assertEqual(_seconds_left(None, 1), 1)
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        frozen = 1_760_000_000.0
        with mock.patch("crank.views.job_matches.time.time", return_value=frozen), \
                mock.patch("crank.views.job_matches.cache") as refused, \
                mock.patch(RECOMPUTE) as recompute:
            refused.add.return_value = False
            refused.get.return_value = None
            response = self.client.post(URL)
        recompute.assert_not_called()
        refused.add.assert_called_once_with(
            f"job-match-refresh:{self.owner.pk}", frozen + 30, 30
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"status": "rate_limited", "retry_after": 1})
        self.assertEqual(response["Retry-After"], "1")

    def test_the_last_second_of_the_window_reads_one_and_then_the_window_ends(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        frozen = 1_760_000_000.0
        with mock.patch("crank.views.job_matches.time.time", return_value=frozen), \
                mock.patch(RECOMPUTE, return_value=mock.Mock(status=RecomputeStatus.FAILED)):
            self.assertEqual(self.client.post(URL).json(), {"status": "failed"})
        for elapsed, left in ((0, 30), (0.5, 30), (29.5, 1), (29.999, 1)):
            with self.subTest(elapsed=elapsed), mock.patch(
                "crank.views.job_matches.time.time", return_value=frozen + elapsed
            ), mock.patch(RECOMPUTE) as recompute:
                response = self.client.post(URL)
            recompute.assert_not_called()
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response["Retry-After"], str(left))
        # The cache shares the frozen clock: once the window is over the entry
        # is gone and the next request runs, it is not told to wait again.
        with mock.patch("crank.views.job_matches.time.time", return_value=frozen + 30.001), \
                mock.patch(
                    RECOMPUTE, return_value=mock.Mock(status=RecomputeStatus.PUBLISHED)
                ) as recompute:
            self.assertEqual(self.client.post(URL).json(), {"status": "published"})
        recompute.assert_called_once()

    def test_the_cooldown_is_per_user(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        self.assertEqual(self.client.post(URL).json(), {"status": "published"})
        other = Client()
        other.force_login(self.other)
        self.assertEqual(other.post(URL).json(), {"status": "published"})

    @override_settings(JOB_MATCH_REFRESH_COOLDOWN_SECONDS=0)
    def test_the_cooldown_cannot_be_configured_away(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        with mock.patch(RECOMPUTE, return_value=mock.Mock(status=RecomputeStatus.FAILED)):
            self.assertEqual(self.client.post(URL).json(), {"status": "failed"})
            response = self.client.post(URL)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["retry_after"], 1)

    def test_concurrent_requests_run_one_recompute(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        real = recompute_user
        during = []

        def recompute_while_another_request_arrives(user, **kwargs):
            # A second request lands while the first is still computing.
            during.append(self.client.post(URL))
            return real(user, **kwargs)

        with mock.patch(RECOMPUTE, side_effect=recompute_while_another_request_arrives) as recompute:
            first = self.client.post(URL)
        self.assertEqual(recompute.call_count, 1)
        self.assertEqual(first.json(), {"status": "published"})
        self.assertEqual([response.status_code for response in during], [429])
        self.assertEqual(during[0].json()["status"], "rate_limited")

    def test_the_response_carries_a_status_and_nothing_else(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        published = self.client.post(URL)
        self.assertEqual(set(published.json()), {"status"})
        self.assertEqual(set(self.client.post(URL).json()), {"status"})
        self.assertNotIn(self.other.username, published.content.decode())

    def test_a_refresh_records_no_telemetry_event(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        with mock.patch("crank.services.monitoring.record_event") as record_event:
            self.assertEqual(self.client.post(URL).json(), {"status": "published"})
        record_event.assert_not_called()

    def test_query_count_is_bounded_and_independent_of_other_users(self):
        CompanyFieldEvidence.objects.filter(pk=self.evidence.pk).delete()
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.post(URL).json(), {"status": "published"})
        self.assertLessEqual(len(queries), 40)
        statements = " ".join(query["sql"] for query in queries)
        self.assertNotIn(f'"user_id" = {self.other.pk}', statements)
