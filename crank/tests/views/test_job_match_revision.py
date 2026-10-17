# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for GET /api/job-matches/revision/ (issue #485)."""
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from crank.agents.jobs.matching import DEFAULT_CONFIG
from crank.models.job_match import MatchResultState
from crank.models.preference import UserPreference
from crank.services import publication

URL = "/api/job-matches/revision/"
KEYS = {
    "result_generation",
    "pending",
    "stale",
    "preference_revision",
    "generated_at",
    "poll_after_seconds",
}
NOW = timezone.now()


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    MATCH_RESULTS_READ_ENABLED=True,
    MATCH_RECOMPUTE_ENABLED=False,
    RESULT_REVISION_POLL_SECONDS=60,
)
class JobMatchRevisionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("owner", password="secret")
        self.other = User.objects.create_user("other", password="secret")
        self.pref = UserPreference.objects.create(user=self.user, revision=3)
        self.state = self.publish(self.user, self.pref, generation=7)

    def publish(self, user, pref, *, generation, **extra):
        fields = {
            "issued_generation": generation,
            "current_generation": generation,
            "preference_revision": pref.revision,
            "preference_version": pref.schema_version,
            "ranker_version": DEFAULT_CONFIG.version,
            "data_revision": publication.data_watermark(),
            "generated_at": NOW,
        }
        fields.update(extra)
        return MatchResultState.objects.create(user=user, **fields)

    def get(self):
        self.client.force_login(self.user)
        return self.client.get(URL)

    def test_anonymous_requests_are_redirected_to_login(self):
        response = Client().get(URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_only_get_is_allowed(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(URL).status_code, 405)

    def test_answers_the_committed_generation(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "result_generation": 7,
                "pending": False,
                "stale": False,
                "preference_revision": 3,
                "generated_at": NOW.isoformat(),
                "poll_after_seconds": 60,
            },
        )

    def test_the_answer_is_private_and_not_stored(self):
        response = self.get()
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_owner_scope(self):
        other_pref = UserPreference.objects.create(user=self.other, revision=9)
        self.publish(self.other, other_pref, generation=42)
        self.assertEqual(self.get().json()["result_generation"], 7)
        self.client.force_login(self.other)
        payload = self.client.get(URL).json()
        self.assertEqual(payload["result_generation"], 42)
        self.assertEqual(payload["preference_revision"], 9)

    def test_user_without_a_generation_gets_nulls(self):
        self.state.delete()
        payload = self.get().json()
        self.assertEqual(set(payload), KEYS)
        self.assertIsNone(payload["result_generation"])
        self.assertIsNone(payload["poll_after_seconds"])

    def test_state_that_never_published_gets_nulls(self):
        MatchResultState.objects.filter(pk=self.state.pk).update(
            current_generation=None
        )
        payload = self.get().json()
        self.assertIsNone(payload["result_generation"])
        self.assertIsNone(payload["poll_after_seconds"])

    def test_user_without_a_preference_row_gets_nulls(self):
        UserPreference.objects.filter(pk=self.pref.pk).delete()
        MatchResultState.objects.get_or_create(
            user=self.user, defaults={"current_generation": 7, "issued_generation": 7}
        )
        payload = self.get().json()
        self.assertIsNone(payload["result_generation"])
        self.assertIsNone(payload["poll_after_seconds"])

    @override_settings(MATCH_RESULTS_READ_ENABLED=False)
    def test_gate_off_answers_nulls(self):
        payload = self.get().json()
        self.assertEqual(
            payload,
            {
                "result_generation": None,
                "pending": False,
                "stale": False,
                "preference_revision": None,
                "generated_at": None,
                "poll_after_seconds": None,
            },
        )

    def test_the_capability_switch_turns_the_read_off(self):
        from crank.models.monitoring import CapabilitySwitch

        CapabilitySwitch.objects.update_or_create(
            key="match_results_read", defaults={"enabled": False}
        )
        self.assertIsNone(self.get().json()["poll_after_seconds"])

    @override_settings(RESULT_REVISION_POLL_SECONDS=0)
    def test_zero_seconds_disables_polling_for_everyone(self):
        payload = self.get().json()
        self.assertIsNone(payload["poll_after_seconds"])
        self.assertEqual(payload["result_generation"], 7)

    @override_settings(RESULT_REVISION_POLL_SECONDS=-5)
    def test_a_negative_setting_reads_as_off(self):
        self.assertIsNone(self.get().json()["poll_after_seconds"])

    @override_settings(RESULT_REVISION_POLL_SECONDS=120)
    def test_the_interval_comes_from_the_setting(self):
        self.assertEqual(self.get().json()["poll_after_seconds"], 120)

    @override_settings(MATCH_RECOMPUTE_ENABLED=True)
    def test_a_run_in_flight_is_pending_and_stale(self):
        MatchResultState.objects.filter(pk=self.state.pk).update(issued_generation=8)
        payload = self.get().json()
        self.assertTrue(payload["stale"])
        self.assertTrue(payload["pending"])
        self.assertEqual(payload["result_generation"], 7)

    def test_a_run_in_flight_is_stale_but_not_pending_with_recompute_off(self):
        MatchResultState.objects.filter(pk=self.state.pk).update(issued_generation=8)
        payload = self.get().json()
        self.assertTrue(payload["stale"])
        self.assertFalse(payload["pending"])

    def test_stale_by_the_data_watermark(self):
        from crank.models.publication import PublicationEvent

        PublicationEvent.objects.create(
            target_type=PublicationEvent.TargetType.ORGANIZATION, target_id=1,
            event_kind=PublicationEvent.EventKind.CHANGED,
        )
        payload = self.get().json()
        self.assertTrue(payload["stale"])
        self.assertFalse(payload["pending"])

    def test_stale_by_the_preference_revision(self):
        UserPreference.objects.filter(pk=self.pref.pk).update(revision=4)
        payload = self.get().json()
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["preference_revision"], 3)

    def test_stale_by_the_ranker_version(self):
        MatchResultState.objects.filter(pk=self.state.pk).update(ranker_version="0.0.1")
        self.assertTrue(self.get().json()["stale"])

    def test_the_request_is_bounded_to_four_queries(self):
        from crank.models.publication import PublicationEvent

        PublicationEvent.objects.create(
            target_type=PublicationEvent.TargetType.ORGANIZATION, target_id=1,
            event_kind=PublicationEvent.EventKind.CHANGED,
        )
        MatchResultState.objects.filter(pk=self.state.pk).update(issued_generation=8)
        self.client.force_login(self.user)
        with override_settings(MATCH_RECOMPUTE_ENABLED=True):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(URL).status_code, 200)
        # The session and the request user are read by authentication, not by
        # the view; the worst case (stale, recompute on) is four.
        own = [
            q["sql"]
            for q in queries
            if "django_session" not in q["sql"]
            and 'FROM "auth_user" WHERE' not in q["sql"]
        ]
        self.assertEqual(len(own), 4, own)

    def test_no_monitoring_event_is_recorded(self):
        self.client.force_login(self.user)
        with mock.patch("crank.services.monitoring.record_event") as record_event:
            self.assertEqual(self.client.get(URL).status_code, 200)
        record_event.assert_not_called()

    def test_the_session_is_not_modified(self):
        self.client.force_login(self.user)
        key = self.client.session.session_key
        before = Session.objects.values_list("expire_date", "session_data").get(
            session_key=key
        )
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        # `client.session` builds a fresh store on every access, so its
        # `modified` flag proves nothing: compare the stored row.
        self.assertEqual(
            Session.objects.values_list("expire_date", "session_data").get(
                session_key=key
            ),
            before,
        )
        self.assertNotIn("sessionid", response.cookies)
        self.assertNotIn("Set-Cookie", response)

    def test_the_read_writes_nothing(self):
        before = (
            MatchResultState.objects.values_list("pk", "issued_generation", "modified")
            .order_by("pk")
            .get()
        )
        self.get()
        after = (
            MatchResultState.objects.values_list("pk", "issued_generation", "modified")
            .order_by("pk")
            .get()
        )
        self.assertEqual(before, after)
