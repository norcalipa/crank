# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
Tests for the Phase 1 authenticated job-search chat transport.

Covers auth, ownership, CSRF, malformed/oversized payloads, idempotent retry,
service errors, rate limiting, and no cross-user leakage.
"""
import json
import threading
import time
import unittest
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crank.agents.job_search.demo import (
    AssistantUnavailable,
    JobSearchService,
    JobSearchServiceError,
    ServiceCostLimit,
    ServiceInvalidOutput,
    ServiceTimeout,
)
from crank.models import (
    JobSearchConversation,
    JobSearchMessage,
    JobSearchTurn,
    UserPreference,
)
from crank.services import preferences
from crank.views import job_search as job_search_views


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class _FakeResults:
    """Minimal results object; only serialization shape is used by the view."""

    def to_json_dict(self) -> dict:
        return {"jobs": [], "organizations": []}


@override_settings(CACHES=LOCMEM)
class JobSearchApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.alice)

    def tearDown(self):
        cache.clear()

    # -- helpers ---------------------------------------------------------
    def _post_json(self, url, payload):
        return self.client.post(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def _start_conversation(self, create_new=True):
        resp = self._post_json(
            reverse("agent-conversation-list"), {"create_new": create_new}
        )
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def _submit(self, conversation_id, content, key):
        return self._post_json(
            reverse("agent-conversation-detail", args=[conversation_id]),
            {"content": content, "idempotency_key": key},
        )

    @staticmethod
    def _uuid(unused):
        return str(uuid.uuid4())

    # -- auth & ownership -------------------------------------------------
    def test_anonymous_users_are_rejected(self):
        self.client.logout()
        resp = self.client.get(reverse("agent-conversation-list"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

        resp = self._post_json(reverse("agent-conversation-list"), {"create_new": True})
        self.assertEqual(resp.status_code, 302)

        resp = self.client.get(reverse("agent-conversation-detail", args=[1]))
        self.assertEqual(resp.status_code, 302)

    def test_users_cannot_access_another_users_conversation(self):
        conversation_id = self._start_conversation()

        self.client.logout()
        self.client.force_login(self.bob)

        resp = self.client.get(
            reverse("agent-conversation-detail", args=[conversation_id])
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["type"], "not_found")

        resp = self._submit(conversation_id, "hello", self._uuid(1))
        self.assertEqual(resp.status_code, 404)

        resp = self.client.get(
            reverse("agent-conversation-export", args=[conversation_id])
        )
        self.assertEqual(resp.status_code, 404)

    def test_reports_nonexistent_conversation_as_404(self):
        resp = self.client.get(reverse("agent-conversation-detail", args=[99999]))
        self.assertEqual(resp.status_code, 404)

    # -- happy path / persistence -----------------------------------------
    def test_mocked_multiturn_flow_and_persistence_after_reload(self):
        conversation_id = self._start_conversation()
        self.assertEqual(JobSearchConversation.objects.count(), 1)

        first = self._submit(conversation_id, "I prefer remote work", self._uuid(1))
        self.assertEqual(first.status_code, 201)
        body = first.json()
        self.assertEqual(body["message"]["role"], "assistant")
        self.assertTrue(body["preferences_changed"])

        second = self._submit(
            conversation_id, "What about compensation?", self._uuid(2)
        )
        self.assertEqual(second.status_code, 201)

        # Simulate a page reload with a fresh, authenticated session.
        self.client.logout()
        reloaded = Client()
        reloaded.force_login(self.alice)

        resume = reloaded.get(reverse("agent-conversation-list"))
        self.assertEqual(resume.status_code, 200)
        history = resume.json()
        self.assertEqual(history["id"], conversation_id)
        roles = [m["role"] for m in history["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        self.assertTrue(history["preferences_changed"])

    @patch("crank.views.job_search.monitoring.record_event")
    def test_helpfulness_gap_emitted_after_many_resultless_turns(self, record):
        # The demo provider never produces result cards; after several turns
        # the conversation trips the helpfulness-gap telemetry (issue #397).
        conversation_id = self._start_conversation()
        for i in range(4):
            self._submit(conversation_id, f"turn {i}", self._uuid(i))
        gap_events = [
            call
            for call in record.call_args_list
            if call.args[0] == "job_search_helpfulness_gap"
        ]
        # First-crossing (issue #423): exactly one event, on the turn where
        # the conversation first becomes a gap -- not on every resultless
        # turn after the threshold.
        self.assertEqual(len(gap_events), 1)
        event_type, attrs = gap_events[0].args
        self.assertEqual(event_type, "job_search_helpfulness_gap")
        self.assertTrue(attrs["empty_result"])
        self.assertEqual(attrs["turns_without_result"], 3)

        # A further resultless turn does not re-emit: the signal is a
        # per-conversation first-crossing, not an every-turn counter.
        record.reset_mock()
        self._submit(conversation_id, "still nothing", self._uuid(99))
        gap_events = [
            call
            for call in record.call_args_list
            if call.args[0] == "job_search_helpfulness_gap"
        ]
        self.assertEqual(gap_events, [])

        # A conversation that produced a result card never fires the gap.
        record.reset_mock()
        with patch.object(
            JobSearchService, "run_turn", autospec=True,
            side_effect=lambda self, conversation, user_message: (
                "Here's a match", False, _FakeResults(),
            ),
        ):
            self._submit(conversation_id, "give me matches", self._uuid(99))
        gap_events = [
            call
            for call in record.call_args_list
            if call.args[0] == "job_search_helpfulness_gap"
        ]
        self.assertEqual(gap_events, [])

    def test_create_new_starts_fresh_conversation(self):
        first_id = self._start_conversation(create_new=True)
        self._submit(first_id, "hello", self._uuid(1))
        second_id = self._start_conversation(create_new=True)
        self.assertNotEqual(first_id, second_id)
        # Old conversation is closed, new one is empty.
        self.assertFalse(
            JobSearchConversation.objects.get(pk=first_id).active
        )
        self.assertEqual(
            JobSearchConversation.objects.get(pk=second_id).messages.count(), 0
        )

    # -- CSRF & validation -------------------------------------------------
    def test_csrf_enforced_on_post(self):
        conversation_id = self._start_conversation()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.alice)
        resp = csrf_client.post(
            reverse("agent-conversation-detail", args=[conversation_id]),
            data=json.dumps({"content": "hi", "idempotency_key": self._uuid(1)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_malformed_json_returns_stable_error(self):
        conversation_id = self._start_conversation()
        resp = self.client.post(
            reverse("agent-conversation-detail", args=[conversation_id]),
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "malformed_json")
        self.assertIn("request_id", body["error"])
        self.assertTrue(resp.headers.get("X-Request-ID"))

    @override_settings(JOB_SEARCH_REQUEST_MAX_BYTES=100)
    def test_oversized_request_body_rejected(self):
        conversation_id = self._start_conversation()
        big = "x" * 500
        resp = self._submit(conversation_id, big, self._uuid(1))
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json()["error"]["type"], "payload_too_large")

    @override_settings(JOB_SEARCH_MESSAGE_MAX_LEN=10)
    def test_oversized_message_rejected(self):
        conversation_id = self._start_conversation()
        resp = self._submit(conversation_id, "a" * 50, self._uuid(1))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["type"], "invalid_message")

    def test_empty_and_missing_fields_rejected(self):
        conversation_id = self._start_conversation()
        resp = self._submit(conversation_id, "", self._uuid(1))
        self.assertEqual(resp.status_code, 400)

        resp = self._post_json(
            reverse("agent-conversation-detail", args=[conversation_id]),
            {"content": "hi"},
        )
        self.assertEqual(resp.status_code, 400)

    # -- idempotency & service errors -------------------------------------
    def test_idempotent_retry_does_not_duplicate_messages(self):
        conversation_id = self._start_conversation()
        key = self._uuid(7)

        first = self._submit(conversation_id, "unique turn", key)
        self.assertEqual(first.status_code, 201)
        first_reply = first.json()["message"]["content"]

        second = self._submit(conversation_id, "unique turn", key)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["message"]["content"], first_reply)

        messages = JobSearchConversation.objects.get(pk=conversation_id).messages
        self.assertEqual(messages.filter(role="user").count(), 1)
        self.assertEqual(messages.filter(role="assistant").count(), 1)

    def test_service_error_is_stable_and_retry_does_not_duplicate(self):
        conversation_id = self._start_conversation()
        key = self._uuid(8)
        real_run_turn = JobSearchService().run_turn.__func__
        failures = {"count": 0}

        def flaky_run_turn(*args, **kwargs):
            if failures["count"] == 0:
                failures["count"] += 1
                raise JobSearchServiceError("boom")
            return real_run_turn(*args, **kwargs)

        with patch.object(
            JobSearchService, "run_turn", autospec=True, side_effect=flaky_run_turn
        ):
            resp = self._submit(conversation_id, "will eventually succeed", key)
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["error"]["type"], "service_error")
        self.assertTrue(resp.headers.get("X-Request-ID"))

        # User message persisted once; no assistant yet.
        conv = JobSearchConversation.objects.get(pk=conversation_id)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 0)

        # Retry with the same key succeeds and does not duplicate the user turn.
        with patch.object(
            JobSearchService, "run_turn", autospec=True, side_effect=real_run_turn
        ):
            retry = self._submit(conversation_id, "will eventually succeed", key)
        self.assertEqual(retry.status_code, 201)
        conv = JobSearchConversation.objects.get(pk=conversation_id)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 1)

    # -- rate limiting -----------------------------------------------------
    @override_settings(JOB_SEARCH_RATE_LIMIT_PER_HOUR=2)
    def test_rate_limit_returns_stable_429(self):
        conversation_id = self._start_conversation()
        self.assertEqual(self._submit(conversation_id, "one", self._uuid(1)).status_code, 201)
        self.assertEqual(self._submit(conversation_id, "two", self._uuid(2)).status_code, 201)
        throttled = self._submit(conversation_id, "three", self._uuid(3))
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(throttled.json()["error"]["type"], "rate_limited")

    @override_settings(JOB_SEARCH_RATE_LIMIT_PER_HOUR=1)
    def test_idempotent_retry_does_not_consume_rate_limit_budget(self):
        """Retries with the same idempotency key after a successful turn
        must not count against the rate-limit budget."""
        conversation_id = self._start_conversation()
        key = self._uuid(42)

        # First submission succeeds and consumes the only budget slot.
        first = self._submit(conversation_id, "one and only", key)
        self.assertEqual(first.status_code, 201)

        # A new key would be throttled (budget exhausted).
        throttled = self._submit(conversation_id, "new message", self._uuid(43))
        self.assertEqual(throttled.status_code, 429)

        # But the idempotent retry of the already-completed turn is free.
        retry = self._submit(conversation_id, "one and only", key)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["message"]["content"], first.json()["message"]["content"])

    @override_settings(JOB_SEARCH_RATE_LIMIT_PER_HOUR=1)
    def test_retry_after_service_error_consumes_rate_limit_budget(self):
        """A failed-turn retry runs the provider again, so it consumes the
        rate-limit budget exactly like a fresh send (adversarial review:
        same-key retries were previously a free, unbounded provider path).
        Only the completed-turn replay stays free."""
        conversation_id = self._start_conversation()
        key = self._uuid(99)
        real_run_turn = JobSearchService().run_turn.__func__

        # First attempt fails the provider but consumes the only budget slot.
        with patch.object(
            JobSearchService, "run_turn", autospec=True,
            side_effect=JobSearchServiceError("boom"),
        ):
            resp = self._submit(conversation_id, "fails once", key)
        self.assertEqual(resp.status_code, 500)

        # The same-key retry would invoke the provider again, so it must be
        # throttled without a provider call and without persisting anything.
        run_calls = {"n": 0}

        def counting_run_turn(*args, **kwargs):
            run_calls["n"] += 1
            return real_run_turn(*args, **kwargs)

        with patch.object(
            JobSearchService, "run_turn", autospec=True,
            side_effect=counting_run_turn,
        ):
            retry = self._submit(conversation_id, "fails once", key)
        self.assertEqual(retry.status_code, 429)
        self.assertEqual(retry.json()["error"]["type"], "rate_limited")
        self.assertEqual(run_calls["n"], 0)
        # The persisted failed turn is untouched and still retryable.
        turn = JobSearchTurn.objects.get(
            conversation_id=conversation_id, turn_key=key
        )
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.FAILED)
        self.assertEqual(turn.attempt_count, 1)

    @override_settings(JOB_SEARCH_RATE_LIMIT_PER_HOUR=2)
    def test_retry_after_service_error_succeeds_within_budget(self):
        """With budget for both attempts the same-key retry succeeds and
        consumes the second slot; the next provider-bound request throttles."""
        conversation_id = self._start_conversation()
        key = self._uuid(101)
        real_run_turn = JobSearchService().run_turn.__func__

        with patch.object(
            JobSearchService, "run_turn", autospec=True,
            side_effect=JobSearchServiceError("boom"),
        ):
            first = self._submit(conversation_id, "fails then works", key)
        self.assertEqual(first.status_code, 500)

        with patch.object(
            JobSearchService, "run_turn", autospec=True,
            side_effect=real_run_turn,
        ):
            retry = self._submit(conversation_id, "fails then works", key)
        self.assertEqual(retry.status_code, 201)

        # Both budget slots are consumed: the next send is throttled.
        throttled = self._submit(conversation_id, "new", self._uuid(102))
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(throttled.json()["error"]["type"], "rate_limited")

    @override_settings(JOB_SEARCH_RATE_LIMIT_PER_HOUR=1)
    def test_rate_limit_consumed_on_first_attempt_not_retry(self):
        """The rate limit is consumed on the *first* attempt with a new key.
        An idempotent retry of the same key after success is free."""
        conversation_id = self._start_conversation()
        key = self._uuid(55)

        # First submission with this key consumes the budget.
        first = self._submit(conversation_id, "budget test", key)
        self.assertEqual(first.status_code, 201)

        # A new key is throttled.
        self.assertEqual(
            self._submit(conversation_id, "blocked", self._uuid(56)).status_code,
            429,
        )

        # Retry of the completed key is free (idempotent replay).
        retry = self._submit(conversation_id, "budget test", key)
        self.assertEqual(retry.status_code, 200)

    # -- export / reset / delete ------------------------------------------
    def test_export_returns_owned_history(self):
        conversation_id = self._start_conversation()
        self._submit(conversation_id, "exportable", self._uuid(1))
        resp = self.client.get(reverse("agent-conversation-export", args=[conversation_id]))
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["user"], "alice")
        self.assertEqual(len(payload["conversation"]["messages"]), 2)

    def test_reset_archives_conversation_and_starts_new(self):
        conversation_id = self._start_conversation()
        self._submit(conversation_id, "before reset", self._uuid(1))
        resp = self.client.post(reverse("agent-conversation-reset", args=[conversation_id]))
        self.assertEqual(resp.status_code, 201)
        new_id = resp.json()["id"]
        self.assertNotEqual(new_id, conversation_id)
        self.assertEqual(JobSearchConversation.objects.get(pk=conversation_id).active, False)

    def test_delete_removes_conversation_and_messages(self):
        conversation_id = self._start_conversation()
        self._submit(conversation_id, "to delete", self._uuid(1))
        resp = self.client.post(reverse("agent-conversation-delete", args=[conversation_id]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(JobSearchConversation.objects.filter(pk=conversation_id).exists())
        self.assertFalse(JobSearchMessage.objects.filter(conversation_id=conversation_id).exists())

@override_settings(CACHES=LOCMEM)
class JobSearchCoverageEdges(TestCase):
    """Direct unit coverage for service/model/serializer/ratelimit edges."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("edge", "edge@example.com", "pw")

    def tearDown(self):
        cache.clear()

    @override_settings(JOB_SEARCH_PROVIDER="bogus")
    def test_unknown_provider_raises_stable_error(self):
        from crank.agents.job_search.demo import _build_provider
        with self.assertRaises(JobSearchServiceError):
            _build_provider()

    def test_service_run_turn_success_and_truncation(self):
        from crank.agents.job_search.demo import DemoJobSearchProvider
        from crank.agents.job_search.quality import is_echo
        conv = JobSearchConversation.objects.create(owner=self.user)
        svc = JobSearchService(DemoJobSearchProvider())
        reply, changed, results = svc.run_turn(conversation=conv, user_message="salary")
        self.assertTrue(reply)
        # NIT-4: verify the reply is genuinely non-echo, not just non-empty.
        self.assertFalse(is_echo("salary", reply))
        self.assertTrue(changed)
        JobSearchMessage.objects.create(
            conversation=conv, role="user", content="salary"
        )
        JobSearchMessage.objects.create(
            conversation=conv, role="user", content="culture"
        )
        reply2, _, _ = svc.run_turn(conversation=conv, user_message="more")
        self.assertIn("more", reply2)
        with override_settings(JOB_SEARCH_RESPONSE_MAX_LEN=10):
            reply3, _, _ = svc.run_turn(conversation=conv, user_message="again")
            self.assertLessEqual(len(reply3), 10)

    def test_provider_failure_is_stable_service_error(self):
        class BoomProvider:
            def generate_reply(self, **kwargs):
                raise RuntimeError("provider exploded")
        conv = JobSearchConversation.objects.create(owner=self.user)
        svc = JobSearchService(BoomProvider())
        with self.assertRaises(JobSearchServiceError):
            svc.run_turn(conversation=conv, user_message="hi")

    def test_models_str_do_not_leak(self):
        conv = JobSearchConversation.objects.create(owner=self.user)
        msg = JobSearchMessage.objects.create(
            conversation=conv, role="user", content="secret"
        )
        self.assertIn("JobSearchConversation", str(conv))
        self.assertIn("JobSearchMessage", str(msg))
        self.assertNotIn("secret", str(msg))

    def test_message_serializer_rejects_blank_content(self):
        from crank.serializers import job_search as jser
        try:
            jser.MessageSubmitSerializer().validate_content("")
            self.fail("Expected ValidationError for blank content")
        except Exception as exc:
            self.assertIn("required", str(exc).lower())

    def test_rate_limit_allows_anonymous(self):
        from crank.views.job_search import _check_rate_limit
        from django.test import RequestFactory
        anon = Client()
        req = RequestFactory().post("/")
        req.user = type("Anonymous", (), {"is_authenticated": False})()
        self.assertFalse(_check_rate_limit(req))

    def test_rate_limit_valueerror_recovery(self):
        """When cache.incr raises ValueError (key expired between add+incr),
        the rate limiter recovers by re-initialising the key."""
        from unittest.mock import patch
        from crank.views.job_search import _check_rate_limit
        from django.test import RequestFactory

        req = RequestFactory().post("/")
        req.user = self.user
        req.META["REMOTE_ADDR"] = "10.0.0.1"

        # Simulate incr failing once (key expired), then succeeding.
        with patch(
            "crank.views.job_search.cache.incr",
            side_effect=[ValueError, 1],
        ):
            self.assertFalse(_check_rate_limit(req))


class JobSearchViewEdgeCases(TestCase):
    """Additional view branches for the 99.25% patch target."""

    @override_settings(CACHES=LOCMEM)
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("edge2", "edge2@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)

    @override_settings(CACHES=LOCMEM)
    def tearDown(self):
        cache.clear()

    def test_list_get_no_active_conversation_404(self):
        resp = self.client.get(reverse("agent-conversation-list"))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["type"], "no_conversation")

    def test_list_post_nonobject_body_400(self):
        resp = self.client.post(
            reverse("agent-conversation-list"),
            data="[1,2,3]",
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_list_post_invalid_create_new_400(self):
        resp = self.client.post(
            reverse("agent-conversation-list"),
            data='{"create_new": "maybe"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["type"], "invalid_request")

    def test_list_post_resume_existing_returns_200(self):
        first = self.client.post(
            reverse("agent-conversation-list"),
            data='{"create_new": true}',
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 201)
        resumed = self.client.post(
            reverse("agent-conversation-list"),
            data='{"create_new": false}',
            content_type="application/json",
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["id"], first.json()["id"])

    def test_detail_get_existing_200(self):
        cid = self.client.post(
            reverse("agent-conversation-list"), data="{}", content_type="application/json"
        ).json()["id"]
        resp = self.client.get(reverse("agent-conversation-detail", args=[cid]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], cid)

    def test_reset_nonexistent_404(self):
        resp = self.client.post(reverse("agent-conversation-reset", args=[999999]))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["type"], "not_found")

    def test_delete_nonexistent_404(self):
        resp = self.client.post(reverse("agent-conversation-delete", args=[999999]))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["type"], "not_found")

    def test_rate_limit_anon_short_circuit(self):
        from crank.views.job_search import _check_rate_limit
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory
        req = RequestFactory().get("/")
        req.user = AnonymousUser()
        self.assertFalse(_check_rate_limit(req))


@override_settings(CACHES=LOCMEM)
class JobSearchResultsTestCase(TestCase):
    """Tests for structured results persistence and transport (issue #396)."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("results", "results@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()

    def _start_conversation(self):
        resp = self.client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def _submit(self, conversation_id, content, key):
        return self.client.post(
            reverse("agent-conversation-detail", args=[conversation_id]),
            data=json.dumps({"content": content, "idempotency_key": key}),
            content_type="application/json",
        )

    def test_message_includes_results_field(self):
        """The serialize_message helper includes a results field."""
        from crank.serializers.job_search import serialize_message
        conv = JobSearchConversation.objects.create(owner=self.user)
        msg = JobSearchMessage.objects.create(
            conversation=conv, role="assistant", content="hello",
            results_json='{"jobs":[],"organizations":[]}',
        )
        serialized = serialize_message(msg)
        self.assertIn("results", serialized)
        self.assertEqual(serialized["results"], {"jobs": [], "organizations": []})

    def test_message_results_null_when_empty(self):
        """results is null when results_json is empty."""
        from crank.serializers.job_search import serialize_message
        conv = JobSearchConversation.objects.create(owner=self.user)
        msg = JobSearchMessage.objects.create(
            conversation=conv, role="assistant", content="hello",
        )
        serialized = serialize_message(msg)
        self.assertIsNone(serialized["results"])

    def test_message_results_malformed_json_returns_none(self):
        """Malformed results_json returns None, not a crash."""
        from crank.serializers.job_search import serialize_message
        conv = JobSearchConversation.objects.create(owner=self.user)
        msg = JobSearchMessage.objects.create(
            conversation=conv, role="assistant", content="hello",
            results_json='{"bad":',
        )
        serialized = serialize_message(msg)
        self.assertIsNone(serialized["results"])

    def test_serialize_results_helper_with_none(self):
        from crank.serializers.job_search import _serialize_results
        self.assertIsNone(_serialize_results(None))

    def test_serialize_results_helper_with_valid_object(self):
        from crank.serializers.job_search import _serialize_results
        from crank.agents.job_search.types import StructuredResults, OrganizationResult
        sr = StructuredResults(organizations=(OrganizationResult(id=1, name="Test"),))
        d = _serialize_results(sr)
        self.assertEqual(d["organizations"][0]["name"], "Test")

    def test_serialize_results_helper_with_broken_object(self):
        from crank.serializers.job_search import _serialize_results
        class Broken:
            def to_json_dict(self):
                raise ValueError("boom")
        self.assertIsNone(_serialize_results(Broken()))

    def test_results_persisted_via_view_with_mock_provider(self):
        """When the provider returns structured results, the view persists them."""
        from crank.agents.job_search.types import StructuredResults, JobResult
        from unittest.mock import patch
        conv_id = self._start_conversation()
        results = StructuredResults(jobs=(
            JobResult(id=1, title="Engineer", organization_name="Acme",
                      location="SF", remote=True),
        ))
        real_run_turn = JobSearchService().run_turn.__func__
        def mock_run_turn(*args, **kwargs):
            return "Found a job!", False, results
        with patch.object(JobSearchService, "run_turn", autospec=True, side_effect=mock_run_turn):
            resp = self._submit(conv_id, "jobs?", str(uuid.uuid4()))
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertIsNotNone(body["message"]["results"])
        self.assertEqual(len(body["message"]["results"]["jobs"]), 1)
        self.assertEqual(body["message"]["results"]["jobs"][0]["title"], "Engineer")
        # Verify it persisted
        conv = JobSearchConversation.objects.get(pk=conv_id)
        assistant_msgs = conv.messages.filter(role="assistant")
        self.assertTrue(any(r.results_json for r in assistant_msgs))

    def test_oversized_results_truncated_via_view(self):
        """When results exceed the byte cap, results_json is truncated to empty."""
        from crank.agents.job_search.types import StructuredResults, JobResult
        from unittest.mock import patch
        conv_id = self._start_conversation()
        # Create results that exceed 65536 bytes
        big_title = "x" * 10000
        results = StructuredResults(jobs=tuple(
            JobResult(id=i, title=big_title, organization_name="",
                      location="", remote=False) for i in range(10)
        ))
        def mock_run_turn(*args, **kwargs):
            return "Big results", False, results
        with patch.object(JobSearchService, "run_turn", autospec=True, side_effect=mock_run_turn):
            resp = self._submit(conv_id, "big?", str(uuid.uuid4()))
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        # results should be None because results_json was truncated
        self.assertIsNone(body["message"]["results"])

    def test_results_serialization_exception_handled(self):
        """When results.to_json_dict raises, results_json falls back to empty."""
        from unittest.mock import patch, PropertyMock
        conv_id = self._start_conversation()
        class BrokenResults:
            def to_json_dict(self):
                raise ValueError("serialization failed")
        def mock_run_turn(*args, **kwargs):
            return "Reply", False, BrokenResults()
        with patch.object(JobSearchService, "run_turn", autospec=True, side_effect=mock_run_turn):
            resp = self._submit(conv_id, "broken?", str(uuid.uuid4()))
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertIsNone(body["message"]["results"])

    def test_results_persisted_and_reloaded(self):
        """Structured results persist with the message and reload shows them."""
        conv = JobSearchConversation.objects.create(owner=self.user)
        results_json = json.dumps({
            "jobs": [{
                "id": 1, "title": "Engineer", "organization_name": "Acme",
                "location": "SF", "remote": True,
                "compensation": {"min": 100, "max": 200, "currency": "USD", "interval": "year"},
                "canonical_url": "https://acme.example/jobs/1",
                "observed_at": None, "updated_at": None,
            }],
            "organizations": [{
                "id": 1, "name": "Acme", "url": "https://acme.example",
                "funding_round": "A", "rto_policy": "R",
            }],
        })
        JobSearchMessage.objects.create(
            conversation=conv, role="user", content="jobs?",
            idempotency_key="key1",
        )
        JobSearchMessage.objects.create(
            conversation=conv, role="assistant", content="Check these out.",
            idempotency_key="key1", results_json=results_json,
        )
        resp = self.client.get(reverse("agent-conversation-detail", args=[conv.pk]))
        self.assertEqual(resp.status_code, 200)
        messages = resp.json()["messages"]
        assistant_msg = [m for m in messages if m["role"] == "assistant"][0]
        self.assertIsNotNone(assistant_msg["results"])
        self.assertEqual(len(assistant_msg["results"]["jobs"]), 1)
        self.assertEqual(assistant_msg["results"]["jobs"][0]["title"], "Engineer")
        self.assertEqual(len(assistant_msg["results"]["organizations"]), 1)
        self.assertEqual(assistant_msg["results"]["organizations"][0]["name"], "Acme")

    def test_results_truncation_on_oversized(self):
        """Oversized results_json string is handled gracefully."""
        conv = JobSearchConversation.objects.create(owner=self.user)
        # Create a valid but large results JSON
        large_results = json.dumps({
            "jobs": [{"id": i, "title": "x" * 500, "organization_name": "",
                       "location": "", "remote": False} for i in range(50)],
            "organizations": [],
        })
        msg = JobSearchMessage.objects.create(
            conversation=conv, role="assistant", content="big",
            results_json=large_results,
        )
        from crank.serializers.job_search import serialize_message
        serialized = serialize_message(msg)
        # Should parse fine since it's valid JSON
        self.assertIsNotNone(serialized["results"])
        self.assertEqual(len(serialized["results"]["jobs"]), 50)


@override_settings(CACHES=LOCMEM)
class OrchestratorE2ESmokeTests(TestCase):
    """Authenticated end-to-end smoke tests using a fake provider transport.

    Proves the full flow through the Django view layer:

    1. Preference-grounded conversation start
    2. Tools (org catalog, score summaries, job listings) are loaded
    3. Ranked/cited results are returned in the assistant reply
    4. Results are persisted and survive a history reload
    5. No live network calls — all via fake provider transport
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("smokeuser", "smoke@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()

    def _start_conversation(self):
        resp = self.client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def _submit(self, conversation_id, content, key):
        return self.client.post(
            reverse("agent-conversation-detail", args=[conversation_id]),
            data=json.dumps({"content": content, "idempotency_key": key}),
            content_type="application/json",
        )

    def _build_fake_orchestrator_provider(self, gateway_result=None, orgs=None, listings=None):
        """Build an OrchestratorJobSearchProvider wired to a fake gateway.

        All datasources are fully programmable — no database queries, no network.
        """
        from types import SimpleNamespace
        from crank.agents.job_search.gateway import GatewayResponse
        from crank.agents.job_search.service import JobSearchOrchestrator
        from crank.agents.job_search.providers import OrchestratorJobSearchProvider

        class FakeGateway:
            def __init__(self, result):
                self.result = result
                self.requests = []

            def complete(self, request):
                self.requests.append(request)
                return GatewayResponse(
                    text=json.dumps(self.result),
                    usage={"output_tokens": 42},
                )

            def close(self):
                """No-op satisfying the :class:`ProviderGateway` ABC contract."""
                return None

        class FakePrefService:
            def validate_patch(self, patch):
                pass

            def apply_patch(self, patch, expected_modified=None):
                return True

        default_orgs = orgs or [
            SimpleNamespace(id=1, name="Acme Inc", url="https://acme.example",
                            funding_round="A", rto_policy="R"),
            SimpleNamespace(id=2, name="Globex Corp", url="https://globex.example",
                            funding_round="S", rto_policy="H"),
        ]
        default_listings = listings or [
            SimpleNamespace(
                id=100, title="Senior Engineer",
                organization=SimpleNamespace(id=1, name="Acme Inc"),
                location_text="San Francisco, CA", is_remote=True,
                compensation_min=150000, compensation_max=250000,
                compensation_currency="USD", compensation_interval="year",
                canonical_url="https://acme.example/jobs/100",
                last_seen_at="2024-08-01T00:00:00", modified="2024-08-02T00:00:00",
            ),
            SimpleNamespace(
                id=101, title="ML Engineer",
                organization=SimpleNamespace(id=2, name="Globex Corp"),
                location_text="Remote", is_remote=True,
                compensation_min=180000, compensation_max=300000,
                compensation_currency="USD", compensation_interval="year",
                canonical_url="https://globex.example/jobs/101",
                last_seen_at="2024-08-01T00:00:00", modified="2024-08-02T00:00:00",
            ),
        ]

        gw = FakeGateway(
            gateway_result or {
                "message": "I recommend Acme Inc and Globex Corp for remote work.",
                "cited_organization_ids": [1, 2],
                "cited_job_listing_ids": [100, 101],
                "preference_patch": {"replace": {"rto_policy": "R"}},
            }
        )
        orchestrator = JobSearchOrchestrator(
            gateway=gw,
            preference_service=FakePrefService(),
            org_datasource=lambda filters, limit: default_orgs,
            score_datasource=lambda ids, types, limit: [
                {"organization_id": 1, "score_type": "culture", "avg_score": 4.5},
                {"organization_id": 2, "score_type": "culture", "avg_score": 4.0},
            ],
            job_listing_datasource=lambda filters, limit: default_listings,
        )
        return OrchestratorJobSearchProvider(orchestrator=orchestrator), gw

    def test_provider_preference_adapter_persists_to_real_store(self):
        """MAJOR-1: the production preference adapter writes to the real store.

        Ensures the adapter (not a null stub) persists chat preference changes
        to the authenticated user's ``UserPreference`` row and reports
        ``changed`` truthfully, so ``preferences_changed`` is meaningful.
        """
        from crank.agents.job_search.providers import _PreferenceServiceAdapter

        user = User.objects.create_user("prefpersist", "pref@example.com", "pw")
        adapter = _PreferenceServiceAdapter(user)

        adapter.validate_patch({"set": {"compensation.minimum_salary": 180000}})
        self.assertTrue(adapter.apply_patch({"set": {"compensation.minimum_salary": 180000}}))

        # The change actually persisted to the owner-scoped preference store.
        from crank.models.preference import UserPreference
        pref = UserPreference.objects.get(user=user)
        self.assertEqual(pref.preferences["compensation"]["minimum_salary"], 180000)

        # An invalid patch is rejected by the real validator.
        with self.assertRaises(Exception):
            adapter.validate_patch({"set": {"not_a_real_field": 1}})

    def test_provider_wires_user_and_match_service_when_not_injected(self):
        """MAJOR-2: a provider without an injected orchestrator wires owner services.

        Guards the production path: ``generate_reply`` passes ``conversation.owner``
        into the orchestrator and wires a real ``match_service``, so
        ``_load_matches`` does NOT short-circuit and preference-grounded
        matches are loaded for the chat.
        """
        from crank.agents.job_search.providers import (
            OrchestratorJobSearchProvider,
            _PreferenceServiceAdapter,
        )

        user = User.objects.create_user("wireowner", "wire@example.com", "pw")
        conv = JobSearchConversation.objects.create(owner=user)

        provider = OrchestratorJobSearchProvider.__new__(OrchestratorJobSearchProvider)
        provider._fixed_orchestrator = None
        provider._gateway = object()
        provider._preference_service = None
        provider._preference_service_factory = None
        provider._match_service = None
        provider._orchestrator_factory = None
        provider._orchestrators = OrderedDict()

        self.assertIs(provider._resolve_user(conv), user)
        orch = provider._ensure_orchestrator(user)
        self.assertIsNotNone(orch)
        # A second call reuses the cached orchestrator (built once per provider).
        self.assertIs(provider._ensure_orchestrator(user), orch)
        self.assertIs(orch._user, user)
        self.assertTrue(callable(orch._match_service))
        self.assertIsInstance(orch._preference_service, _PreferenceServiceAdapter)

        # The wired match service is the real preference-grounded loader: it
        # returns the documented shape even with no saved prefs, proving it
        # does not short-circuit on a None user.
        match = orch._match_service(user, limit=5)
        self.assertIn("job_matches", match)
        self.assertIn("organization_matches", match)

    def _patch_service_provider(self, provider):
        """Inject ``provider`` into the view's ``JobSearchService``.

        Only ``__init__`` is patched: the real ``run_turn`` glue (error
        wrapping, ``JOB_SEARCH_RESPONSE_MAX_LEN`` truncation, and the
        ``(text, changed, results)`` contract) is exercised end-to-end.
        """
        from unittest.mock import patch
        from crank.agents.job_search.demo import JobSearchService

        return patch.object(
            JobSearchService,
            "__init__",
            lambda self, *args, **kwargs: setattr(self, "provider", provider),
        )

    def test_preference_to_tools_to_ranked_results_to_history_reload(self):
        """Full smoke test: preference → tools → ranked/cited results → history reload.

        Proves that when a user submits a preference-grounded message through
        the real view layer, the orchestrator (backed by a fake gateway) loads
        tools data, produces ranked/cited results, persists them, and the
        results survive a full history reload. The real ``run_turn`` glue runs
        under the injected provider, so error wrapping/truncation are covered.
        """
        provider, gateway = self._build_fake_orchestrator_provider()

        # Inject the fake provider into the view's service; ``run_turn`` runs real.
        with self._patch_service_provider(provider):
            # 1. Start a conversation
            conv_id = self._start_conversation()

            # 2. Submit a preference-grounded message
            key = str(uuid.uuid4())
            resp = self._submit(conv_id, "I want remote work at a seed-stage startup", key)
            self.assertEqual(resp.status_code, 201)
            body = resp.json()

            # 3. Verify the assistant reply is grounded
            self.assertEqual(body["message"]["role"], "assistant")
            self.assertIn("Acme", body["message"]["content"])
            self.assertIn("Globex", body["message"]["content"])
            self.assertTrue(body["preferences_changed"])

            # 4. Verify ranked/cited results are present
            results = body["message"]["results"]
            self.assertIsNotNone(results)
            self.assertIn("organizations", results)
            self.assertIn("jobs", results)

            # Check organizations
            org_names = [o["name"] for o in results["organizations"]]
            self.assertIn("Acme Inc", org_names)
            self.assertIn("Globex Corp", org_names)
            self.assertEqual(len(results["organizations"]), 2)

            # Check jobs
            job_titles = [j["title"] for j in results["jobs"]]
            self.assertIn("Senior Engineer", job_titles)
            self.assertIn("ML Engineer", job_titles)
            self.assertEqual(len(results["jobs"]), 2)

            # Verify job details are complete
            senior = [j for j in results["jobs"] if j["title"] == "Senior Engineer"][0]
            self.assertEqual(senior["organization_name"], "Acme Inc")
            self.assertTrue(senior["remote"])
            self.assertEqual(senior["compensation"]["min"], 150000)
            self.assertEqual(senior["compensation"]["currency"], "USD")
            self.assertEqual(senior["canonical_url"], "https://acme.example/jobs/100")

            # 5. Verify tools were called (conversation context includes org data)
            self.assertGreaterEqual(len(gateway.requests), 1)
            request = gateway.requests[0]
            # The system prompt should contain the organization catalog
            system_content = ""
            for msg in request.messages:
                if msg.get("role") == "system":
                    system_content = msg.get("content", "")
            self.assertIn("Acme Inc", system_content)
            self.assertIn("Globex Corp", system_content)

            # 6. Simulate page reload: fetch conversation history
            self.client.logout()
            reloaded = Client()
            reloaded.force_login(self.user)
            history_resp = reloaded.get(
                reverse("agent-conversation-detail", args=[conv_id])
            )
            self.assertEqual(history_resp.status_code, 200)
            history = history_resp.json()
            self.assertEqual(len(history["messages"]), 2)  # user + assistant

            # 7. Verify results survive the reload
            assistant_msg = [m for m in history["messages"] if m["role"] == "assistant"][0]
            self.assertIsNotNone(assistant_msg["results"])
            reloaded_orgs = [o["name"] for o in assistant_msg["results"]["organizations"]]
            self.assertIn("Acme Inc", reloaded_orgs)
            self.assertIn("Globex Corp", reloaded_orgs)

            # The fake gateway satisfies the ProviderGateway close()/context-
            # manager contract the transport layer relies on (NIT-1); close is
            # idempotent and must not raise.
            gateway.close()
            gateway.close()

    def test_fake_provider_returns_no_citations(self):
        """When the model returns no citations, results are None (not empty lists)."""
        provider, _ = self._build_fake_orchestrator_provider(
            gateway_result={
                "message": "Tell me more about what you're looking for.",
                "cited_organization_ids": [],
                "cited_job_listing_ids": [],
                "preference_patch": None,
            }
        )

        with self._patch_service_provider(provider):
            conv_id = self._start_conversation()
            resp = self._submit(conv_id, "hello", str(uuid.uuid4()))
            self.assertEqual(resp.status_code, 201)
            body = resp.json()
            self.assertIsNone(body["message"]["results"])
            self.assertIn("Tell me more", body["message"]["content"])

    def test_orchestrator_selected_via_provider_setting(self):
        """JOB_SEARCH_PROVIDER=orchestrator with valid LLM config selects the orchestrator."""
        from crank.agents.job_search.demo import _build_provider
        from crank.agents.job_search.providers import OrchestratorJobSearchProvider

        with self.settings(
            JOB_SEARCH_PROVIDER="orchestrator",
            INTERACTIVE_AGENT_ENABLED=True,
            LLM_PROVIDER="crank.agents.llm:FakeLLMProvider",
            LLM_MODEL="",
        ):
            provider = _build_provider()
            self.assertIsInstance(provider, OrchestratorJobSearchProvider)

    def test_orchestrator_fails_closed_when_interactive_agent_disabled(self):
        """Orchestrator path fails closed when INTERACTIVE_AGENT_ENABLED is False."""
        from crank.agents.job_search.demo import _build_provider, JobSearchServiceError

        with self.settings(
            JOB_SEARCH_PROVIDER="orchestrator",
            INTERACTIVE_AGENT_ENABLED=False,
        ):
            with self.assertRaises(JobSearchServiceError):
                _build_provider()

    def test_orchestrator_fails_closed_when_interactive_agent_setting_absent(self):
        """Orchestrator fails closed when INTERACTIVE_AGENT_ENABLED is not defined.

        ``_build_provider`` reads the setting with ``getattr(settings, ...,
        False)``; since ``INTERACTIVE_AGENT_ENABLED`` is not defined in the
        base settings, that fallback path is exercised here (the missing
        branch, distinct from an explicit ``False``). It must fail closed
        rather than silently falling back to the demo provider.
        """
        from crank.agents.job_search.demo import _build_provider, JobSearchServiceError

        with self.settings(JOB_SEARCH_PROVIDER="orchestrator"):
            with self.assertRaises(JobSearchServiceError):
                _build_provider()

    def test_unknown_job_search_provider_fails_closed(self):
        """Unknown JOB_SEARCH_PROVIDER (e.g. a typo) raises, not a silent fallback."""
        from crank.agents.job_search.demo import _build_provider, JobSearchServiceError

        for bad in ("Orchestrator", "orchestaror", "prod"):
            with self.subTest(provider=bad):
                with self.settings(JOB_SEARCH_PROVIDER=bad, INTERACTIVE_AGENT_ENABLED=True):
                    with self.assertRaises(JobSearchServiceError):
                        _build_provider()

    def test_orchestrator_fails_closed_when_llm_provider_empty(self):
        """Orchestrator path fails closed when LLM_PROVIDER is not configured."""
        from crank.agents.job_search.demo import JobSearchServiceError, _build_provider

        with self.settings(
            JOB_SEARCH_PROVIDER="orchestrator",
            INTERACTIVE_AGENT_ENABLED=True,
            LLM_PROVIDER="",
        ), self.assertRaises(JobSearchServiceError):
            _build_provider()

    def test_orchestrator_fails_closed_when_llm_provider_missing_api_key(self):
        """Orchestrator path fails closed when the selected LLM provider requires an API key."""
        from crank.agents.job_search.demo import JobSearchServiceError, _build_provider

        with self.settings(
            JOB_SEARCH_PROVIDER="orchestrator",
            INTERACTIVE_AGENT_ENABLED=True,
            LLM_PROVIDER="crank.agents.llm:OpenAIChatAdapter",
            LLM_API_KEY="",
            LLM_MODEL="gpt-4",
        ), self.assertRaises(JobSearchServiceError):
            _build_provider()

    def test_empty_inventory_still_returns_reply(self):
        """When the inventory is empty (no orgs, no listings), the chat still works."""
        provider, _ = self._build_fake_orchestrator_provider(
            gateway_result={
                "message": "No organizations match your criteria yet. Check back soon!",
                "cited_organization_ids": [],
                "cited_job_listing_ids": [],
                "preference_patch": None,
            },
            orgs=[],
            listings=[],
        )

        with self._patch_service_provider(provider):
            conv_id = self._start_conversation()
            resp = self._submit(conv_id, "any jobs?", str(uuid.uuid4()))
            self.assertEqual(resp.status_code, 201)
            body = resp.json()
            self.assertIsNone(body["message"]["results"])
            self.assertIn("No organizations", body["message"]["content"])

    def test_idempotent_retry_with_orchestrator(self):
        """Idempotent retry works with the orchestrator provider (no duplicate turns).

        Asserts both ``content`` and ``results`` survive the replay so a bug
        that drops structured results on an idempotent retry is caught.
        """
        provider, _ = self._build_fake_orchestrator_provider()

        with self._patch_service_provider(provider):
            conv_id = self._start_conversation()
            key = str(uuid.uuid4())

            first = self._submit(conv_id, "remote work", key)
            self.assertEqual(first.status_code, 201)
            first_body = first.json()
            first_reply = first_body["message"]["content"]
            first_results = first_body["message"]["results"]
            self.assertIsNotNone(first_results)

            second = self._submit(conv_id, "remote work", key)
            self.assertEqual(second.status_code, 200)
            second_body = second.json()
            self.assertEqual(second_body["message"]["content"], first_reply)
            # MINOR-3: results must be preserved on the idempotent replay, not
            # silently dropped while the reply text happens to match.
            self.assertEqual(second_body["message"]["results"], first_results)

            messages = JobSearchConversation.objects.get(pk=conv_id).messages
            self.assertEqual(messages.filter(role="user").count(), 1)
            self.assertEqual(messages.filter(role="assistant").count(), 1)


class HelpfulnessGapConcurrencyTest(TransactionTestCase):
    """Concurrency safety of the one-time helpfulness-gap emission.

    Issue #423 MINOR-2: the previous ``assistant_turns == MIN_HELPFUL_TURNS``
    check was only a derived snapshot, so concurrent submissions could both
    observe the crossing and double-emit (or both observe a later count and
    miss it). The emission is now gated by a durable atomic transition: the
    conditional update that flips ``helpfulness_gap_emitted`` false -> true on
    a single row can succeed exactly once, regardless of how many concurrent
    submissions race it.

    ``TransactionTestCase`` (file-backed sqlite) is used so every worker
    thread uses its own DB connection and contends on the same row, which is
    what a real concurrent crossing looks like.
    """

    def test_concurrent_gap_crossing_emits_exactly_once(self):
        from unittest.mock import patch as _patch

        alice = User.objects.create_user("concurrent", "c@example.com", "pw")
        client = Client()
        client.force_login(alice)
        cache.clear()

        create = client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 201)
        conversation_id = create.json()["id"]

        def post(content, key):
            return client.post(
                reverse("agent-conversation-detail", args=[conversation_id]),
                data=json.dumps({"content": content, "idempotency_key": key}),
                content_type="application/json",
            )

        # Force the gap condition true so that every concurrent submission
        # reaches the one-time claim with the durable flag still unset. The
        # race is then purely on the atomic claim, which is the invariant
        # (issue #423 MINOR-2): exactly one submission may flip the flag.
        with _patch(
            "crank.agents.job_search.quality.has_helpfulness_gap", return_value=True
        ):
            with _patch("crank.views.job_search.monitoring.record_event") as record:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    futures = [
                        pool.submit(post, "race {}".format(i), str(uuid.uuid4()))
                        for i in range(6)
                    ]
                    for future in futures:
                        future.result()

        gap_events = [
            call for call in record.call_args_list
            if call.args[0] == "job_search_helpfulness_gap"
        ]
        self.assertEqual(len(gap_events), 1)
        self.assertTrue(
            JobSearchConversation.objects.get(pk=conversation_id).helpfulness_gap_emitted
        )
        cache.clear()


@override_settings(CACHES=LOCMEM)
class TypedErrorCategoryTests(TestCase):
    """Tests for typed, redaction-safe failure categories (issue #442).

    Each failure category returns a stable JSON envelope with a typed
    ``error.type``, safe user copy, ``request_id``, and the correct HTTP
    status code.  The user turn is persisted before the error so the client
    can retry with the same idempotency key without duplicating.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("typed", "typed@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()

    def _start_conversation(self):
        resp = self.client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def _submit(self, conversation_id, content, key):
        return self.client.post(
            reverse("agent-conversation-detail", args=[conversation_id]),
            data=json.dumps({"content": content, "idempotency_key": key}),
            content_type="application/json",
        )

    @patch("crank.views.job_search.monitoring.record_event")
    def test_assistant_unavailable_returns_503(self, record):
        """Disabled/unconfigured assistant returns 503 with typed envelope."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with patch.object(
            JobSearchService, "__init__",
            side_effect=AssistantUnavailable("not configured"),
        ):
            resp = self._submit(conv_id, "hello", key)
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "assistant_unavailable")
        self.assertIn("request_id", body["error"])
        self.assertNotIn("not configured", body["error"]["message"])
        self.assertTrue(resp.headers.get("X-Request-ID"))
        # Telemetry records the error category without content.
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "assistant_unavailable" for c in calls))

    @patch("crank.views.job_search.monitoring.record_event")
    def test_provider_timeout_returns_504(self, record):
        """Provider timeout returns 504 with typed envelope."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with patch.object(
            JobSearchService, "run_turn",
            side_effect=ServiceTimeout("timed out"),
        ):
            resp = self._submit(conv_id, "hello", key)
        self.assertEqual(resp.status_code, 504)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "provider_timeout")
        self.assertIn("request_id", body["error"])
        self.assertNotIn("timed out", body["error"]["message"])
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "provider_timeout" for c in calls))

    @patch("crank.views.job_search.monitoring.record_event")
    def test_cost_limit_returns_429(self, record):
        """Cost/rate limit returns 429 with typed envelope."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with patch.object(
            JobSearchService, "run_turn",
            side_effect=ServiceCostLimit("over budget"),
        ):
            resp = self._submit(conv_id, "hello", key)
        self.assertEqual(resp.status_code, 429)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "cost_limit")
        self.assertIn("request_id", body["error"])
        self.assertNotIn("over budget", body["error"]["message"])
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "cost_limit" for c in calls))

    @patch("crank.views.job_search.monitoring.record_event")
    def test_invalid_output_returns_500(self, record):
        """Invalid model output returns 500 with typed envelope."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with patch.object(
            JobSearchService, "run_turn",
            side_effect=ServiceInvalidOutput("bad schema"),
        ):
            resp = self._submit(conv_id, "hello", key)
        self.assertEqual(resp.status_code, 500)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "invalid_output")
        self.assertIn("request_id", body["error"])
        self.assertNotIn("bad schema", body["error"]["message"])
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "invalid_output" for c in calls))

    @patch("crank.views.job_search.monitoring.record_event")
    def test_generic_service_error_returns_500(self, record):
        """Generic JobSearchServiceError returns 500 with service_error type."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with patch.object(
            JobSearchService, "run_turn",
            side_effect=JobSearchServiceError("boom"),
        ):
            resp = self._submit(conv_id, "hello", key)
        self.assertEqual(resp.status_code, 500)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "service_error")
        self.assertIn("request_id", body["error"])
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "service_error" for c in calls))

    @patch("crank.views.job_search.monitoring.record_event")
    def test_unexpected_error_returns_500_unexpected_error(self, record):
        """Unexpected non-service exception returns 500 with unexpected_error type."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with patch.object(
            JobSearchService, "run_turn",
            side_effect=ValueError("unexpected bug"),
        ):
            resp = self._submit(conv_id, "hello", key)
        self.assertEqual(resp.status_code, 500)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "unexpected_error")
        self.assertIn("request_id", body["error"])
        self.assertNotIn("unexpected bug", body["error"]["message"])
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "unexpected_error" for c in calls))

    def test_typed_error_preserves_user_turn_for_retry(self):
        """A typed error preserves the user turn so retry with same key works."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        # First attempt: timeout error.
        with patch.object(
            JobSearchService, "run_turn",
            side_effect=ServiceTimeout("timed out"),
        ):
            resp = self._submit(conv_id, "retry me", key)
        self.assertEqual(resp.status_code, 504)
        # User message persisted once.
        conv = JobSearchConversation.objects.get(pk=conv_id)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 0)
        # Retry with same key succeeds.
        real_run = JobSearchService().run_turn.__func__
        with patch.object(JobSearchService, "run_turn", autospec=True, side_effect=real_run):
            retry = self._submit(conv_id, "retry me", key)
        self.assertEqual(retry.status_code, 201)
        conv = JobSearchConversation.objects.get(pk=conv_id)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 1)

    def test_assistant_unavailable_from_build_provider_in_view(self):
        """JobSearchService() construction failure inside the try/except
        returns 503, not an unhandled 500."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        # Patch __init__ to raise AssistantUnavailable (as _build_provider would).
        with patch.object(
            JobSearchService, "__init__",
            side_effect=AssistantUnavailable("disabled"),
        ):
            resp = self._submit(conv_id, "config test", key)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["type"], "assistant_unavailable")

    def test_error_envelope_never_leaks_provider_details(self):
        """The error envelope must never expose provider details, credentials,
        or internal error messages."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        sensitive = "api_key=sk-1234567890 provider=OpenAI model=gpt-4"
        with patch.object(
            JobSearchService, "run_turn",
            side_effect=ServiceTimeout(sensitive),
        ):
            resp = self._submit(conv_id, "leak test", key)
        body = resp.json()
        msg = body["error"]["message"]
        # The safe user copy must not contain the sensitive exception text.
        self.assertNotIn("sk-1234567890", msg)
        self.assertNotIn("api_key", msg)
        self.assertNotIn("OpenAI", msg)
        self.assertNotIn("gpt-4", msg)


@override_settings(CACHES=LOCMEM)
class StalePreferenceAndLateReplyTests(TestCase):
    """Issue #487: optimistic-concurrency wiring and late-reply lifecycle guards.

    Covers three guards end-to-end through the Django view layer:

    1. A turn whose proposed preference patch is stale (the preference row
       changed while the turn was in flight) returns a stable 409
       ``preference_stale``, never overwrites the newer version, and leaves
       the persisted user turn retryable.
    2. A matching version applies the patch normally (both branches of the
       optimistic check).
    3. A reply completing after the conversation was reset or deleted
       mid-turn attaches to no active conversation (409
       ``conversation_closed``) and changes no preferences.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("stale", "stale@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)
        # Materialize the owner's preference row so the version capture has a
        # real ``modified`` timestamp to check against.
        preferences.read(self.user)

    def tearDown(self):
        cache.clear()

    def _start_conversation(self):
        resp = self.client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def _submit(self, conversation_id, content, key):
        return self.client.post(
            reverse("agent-conversation-detail", args=[conversation_id]),
            data=json.dumps({"content": content, "idempotency_key": key}),
            content_type="application/json",
        )

    def _build_orchestrator_provider(self, gateway):
        """Build an OrchestratorJobSearchProvider wired to the real preference store.

        The gateway is caller-controlled so a test can mutate lifecycle state
        mid-turn (inside ``complete``) exactly like a slow real provider.
        """
        from crank.agents.job_search.providers import (
            OrchestratorJobSearchProvider,
            _PreferenceServiceAdapter,
        )
        from crank.agents.job_search.service import JobSearchOrchestrator

        orchestrator = JobSearchOrchestrator(
            gateway=gateway,
            preference_service=_PreferenceServiceAdapter(self.user),
            org_datasource=lambda filters, limit: [],
            score_datasource=lambda ids, types, limit: [],
            job_listing_datasource=lambda filters, limit: [],
        )
        return OrchestratorJobSearchProvider(orchestrator=orchestrator)

    def _patch_service_provider(self, provider):
        return patch.object(
            JobSearchService,
            "__init__",
            lambda self, *args, **kwargs: setattr(self, "provider", provider),
        )

    @patch("crank.views.job_search.monitoring.record_event")
    def test_stale_preference_patch_returns_409_and_never_overwrites(self, record):
        """A late reply's patch is rejected when the row changed mid-turn."""
        from crank.services import preferences as pref_service

        class ConcurrentlyEditingGateway:
            """Simulates a user preference edit racing a slow provider reply."""

            def __init__(self, user):
                self.user = user
                self.payload = {
                    "message": "I noted your preference update.",
                    "cited_organization_ids": [],
                    "cited_job_listing_ids": [],
                    "preference_patch": {"set": {"notes": "late patch"}},
                }

            def complete(self, request):
                # Mid-turn: the user (or another request) edits preferences,
                # bumping the row's ``modified`` past the turn-start capture.
                pref_service.apply_patch_to_user(
                    self.user, {"set": {"notes": "concurrent edit"}}
                )
                from crank.agents.job_search.gateway import GatewayResponse

                return GatewayResponse(text=json.dumps(self.payload))

            def close(self):
                return None

        provider = self._build_orchestrator_provider(
            ConcurrentlyEditingGateway(self.user)
        )
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())

        with self._patch_service_provider(provider):
            resp = self._submit(conv_id, "please prefer remote work", key)

        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "preference_stale")
        self.assertIn("request_id", body["error"])
        self.assertTrue(resp.headers.get("X-Request-ID"))

        # The stale patch was never applied; the concurrent edit survived.
        stored = UserPreference.objects.get(user=self.user)
        self.assertEqual(stored.preferences["notes"], "concurrent edit")

        # The user turn persisted exactly once; no assistant message.
        conv = JobSearchConversation.objects.get(pk=conv_id)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 0)

        # Telemetry records the reason without any content.
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "preference_stale" for c in calls))

        # The persisted user turn stays retryable with the same key.
        from crank.agents.job_search.gateway import GatewayResponse

        class CleanGateway:
            def complete(self, request):
                return GatewayResponse(
                    text=json.dumps(
                        {
                            "message": "Got it — remote work noted.",
                            "cited_organization_ids": [],
                            "cited_job_listing_ids": [],
                            "preference_patch": {"set": {"notes": "retry patch"}},
                        }
                    )
                )

            def close(self):
                return None

        retry_provider = self._build_orchestrator_provider(CleanGateway())
        with self._patch_service_provider(retry_provider):
            retry = self._submit(conv_id, "please prefer remote work", key)
        self.assertEqual(retry.status_code, 201)
        conv = JobSearchConversation.objects.get(pk=conv_id)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 1)
        stored.refresh_from_db()
        self.assertEqual(stored.preferences["notes"], "retry patch")

    def test_matching_preference_version_applies_patch(self):
        """Both branches: a matching ``expected_modified`` applies the patch."""
        from crank.agents.job_search.gateway import GatewayResponse

        class CleanGateway:
            def complete(self, request):
                return GatewayResponse(
                    text=json.dumps(
                        {
                            "message": "Noted.",
                            "cited_organization_ids": [],
                            "cited_job_listing_ids": [],
                            "preference_patch": {"set": {"notes": "accepted patch"}},
                        }
                    )
                )

            def close(self):
                return None

        provider = self._build_orchestrator_provider(CleanGateway())
        conv_id = self._start_conversation()
        with self._patch_service_provider(provider):
            resp = self._submit(conv_id, "prefer remote", str(uuid.uuid4()))
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()["preferences_changed"])
        stored = UserPreference.objects.get(user=self.user)
        self.assertEqual(stored.preferences["notes"], "accepted patch")

    def test_preference_adapter_maps_stale_error_and_versions(self):
        """Unit: the production adapter maps store errors to typed errors.

        Also pins the baseline semantics (issue #487 review, MAJOR-3/MAJOR-4):
        ``PREFERENCE_ABSENT`` applies only while the row stays absent; a row
        deleted mid-turn fails closed instead of being silently re-created.
        """
        from crank.agents.job_search.errors import PreferenceStaleError
        from crank.agents.job_search.providers import _PreferenceServiceAdapter
        from crank.services.preferences import PREFERENCE_ABSENT, StalePreferenceError as _StoreStale

        fresh = User.objects.create_user("adapteruser", "adapter@example.com", "pw")
        adapter = _PreferenceServiceAdapter(fresh)
        self.assertTrue(adapter.writable)

        # No row yet: the ABSENT baseline applies and creates the row.
        self.assertTrue(adapter.apply_patch({"set": {"notes": "v-first"}}, expected_modified=PREFERENCE_ABSENT))
        self.assertEqual(UserPreference.objects.get(user=fresh).preferences["notes"], "v-first")
        version = UserPreference.objects.get(user=fresh).modified

        # Correct version applies; wrong version raises the orchestrator-level
        # typed error (mapped from the store's StalePreferenceError).
        self.assertTrue(
            adapter.apply_patch({"set": {"notes": "v-ok"}}, expected_modified=version)
        )
        self.assertEqual(UserPreference.objects.get(user=fresh).preferences["notes"], "v-ok")
        with self.assertRaises(PreferenceStaleError) as ctx:
            adapter.apply_patch(
                {"set": {"notes": "v-stale"}}, expected_modified=version
            )
        # The underlying store error is a StalePreferenceError (chained).
        self.assertIsInstance(ctx.exception.__cause__, _StoreStale)
        # The stale patch was not applied.
        self.assertEqual(UserPreference.objects.get(user=fresh).preferences["notes"], "v-ok")

        # A row existing at capture but deleted mid-turn fails closed: the
        # patch never re-creates the deleted preference state.
        current = UserPreference.objects.get(user=fresh).modified
        preferences.delete_user_preference(fresh)
        with self.assertRaises(PreferenceStaleError):
            adapter.apply_patch({"set": {"notes": "v-resurrect"}}, expected_modified=current)
        self.assertFalse(UserPreference.objects.filter(user=fresh).exists())

        # ABSENT against a row created after capture is stale, not overwrite.
        preferences.read(fresh)  # the row appears mid-turn, after the capture
        with self.assertRaises(PreferenceStaleError):
            adapter.apply_patch({"set": {"notes": "v-overwrite"}}, expected_modified=PREFERENCE_ABSENT)
        self.assertEqual(
            UserPreference.objects.get(user=fresh).preferences["notes"], ""
        )

    @patch("crank.views.job_search.monitoring.record_event")
    def test_reply_after_midturn_reset_attaches_to_nothing(self, record):
        """A reply completing after a mid-turn reset persists no assistant message."""
        from crank.models.job_search import JobSearchConversation

        class MidTurnResetProvider:
            """Mutates conversation lifecycle state mid-turn (reset case)."""

            def generate_reply(self, *, conversation, user_message):
                JobSearchConversation.objects.filter(pk=conversation.pk).update(
                    active=False
                )
                JobSearchConversation.objects.create(owner=conversation.owner)
                return "late reply after reset", True, None

        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with self._patch_service_provider(MidTurnResetProvider()):
            resp = self._submit(conv_id, "hello", key)

        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "conversation_closed")
        self.assertTrue(resp.headers.get("X-Request-ID"))

        # The old conversation is closed and carries no assistant message; the
        # fresh one created by the reset is empty as well.
        old = JobSearchConversation.objects.get(pk=conv_id)
        self.assertFalse(old.active)
        self.assertEqual(old.messages.filter(role="assistant").count(), 0)
        for conv in JobSearchConversation.objects.filter(owner=self.user):
            self.assertEqual(conv.messages.filter(role="assistant").count(), 0)

        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "conversation_closed" for c in calls))

    @patch("crank.views.job_search.monitoring.record_event")
    def test_reply_after_midturn_delete_attaches_to_nothing(self, record):
        """A reply completing after a mid-turn delete persists no message row."""
        from crank.models.job_search import JobSearchConversation

        class MidTurnDeleteProvider:
            """Mutates conversation lifecycle state mid-turn (delete case)."""

            def generate_reply(self, *, conversation, user_message):
                conversation.messages.all().delete()
                JobSearchConversation.objects.filter(pk=conversation.pk).delete()
                return "late reply after delete", True, None

        conv_id = self._start_conversation()
        with self._patch_service_provider(MidTurnDeleteProvider()):
            resp = self._submit(conv_id, "hello", str(uuid.uuid4()))

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        # No orphan assistant message anywhere.
        self.assertFalse(JobSearchMessage.objects.filter(role="assistant").exists())
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "conversation_closed" for c in calls))

    def test_reply_after_midturn_reset_changes_no_preferences(self):
        """The closed-conversation guard leaves the preference row untouched."""
        from crank.models.job_search import JobSearchConversation

        stored = UserPreference.objects.get(user=self.user)
        before = stored.preferences

        class MidTurnResetProvider:
            def generate_reply(self, *, conversation, user_message):
                # The turn claims a preference change while resetting lifecycle
                # state; neither may reach the user's stored preferences.
                JobSearchConversation.objects.filter(pk=conversation.pk).update(
                    active=False
                )
                return "late reply", True, None

        conv_id = self._start_conversation()
        with self._patch_service_provider(MidTurnResetProvider()):
            resp = self._submit(conv_id, "hello", str(uuid.uuid4()))
        self.assertEqual(resp.status_code, 409)
        stored.refresh_from_db()
        self.assertEqual(stored.preferences, before)
        self.assertEqual(stored.preferences_markdown, preferences.read(self.user)["markdown"])

    @patch("crank.views.job_search.monitoring.record_event")
    def test_midturn_reset_with_proposed_patch_changes_neither_domain(self, record):
        """MAJOR-2 (real orchestrator + real store): a reset landing mid-turn
        with a proposed patch commits NEITHER the patch nor the reply.

        The patch application verifies conversation-active state inside the
        same transaction as the write (the lifecycle guard), so the transport
        returns 409 ``conversation_closed`` while preferences remain
        unchanged — previously the patch committed before the view's
        post-turn check and the user's preferences changed under a closed
        conversation.
        """
        from crank.models.job_search import JobSearchConversation

        class MidTurnResetWithPatchGateway:
            """Resets the conversation mid-turn and proposes a preference patch."""

            def complete(self, request):
                JobSearchConversation.objects.filter(pk=conversation_pk).update(
                    active=False
                )
                JobSearchConversation.objects.create(owner=user)
                from crank.agents.job_search.gateway import GatewayResponse

                return GatewayResponse(
                    text=json.dumps(
                        {
                            "message": "Noted your preference.",
                            "cited_organization_ids": [],
                            "cited_job_listing_ids": [],
                            "preference_patch": {"set": {"notes": "late patch"}},
                        }
                    )
                )

            def close(self):
                return None

        user = self.user
        conversation_pk = self._start_conversation()

        provider = self._build_orchestrator_provider(
            MidTurnResetWithPatchGateway()
        )
        with self._patch_service_provider(provider):
            resp = self._submit(conversation_pk, "prefer remote", str(uuid.uuid4()))

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        # Neither persistence domain changed: no preference patch applied...
        stored = UserPreference.objects.get(user=self.user)
        self.assertEqual(stored.preferences["notes"], "")
        # ...and no assistant message attached to the closed conversation.
        old = JobSearchConversation.objects.get(pk=conversation_pk)
        self.assertFalse(old.active)
        self.assertEqual(old.messages.filter(role="assistant").count(), 0)
        # The persisted user turn stays retryable.
        self.assertEqual(old.messages.filter(role="user").count(), 1)
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "conversation_closed" for c in calls))

    @patch("crank.views.job_search.monitoring.record_event")
    def test_midturn_delete_with_proposed_patch_changes_neither_domain(self, record):
        """MAJOR-2 delete variant: a delete landing mid-turn with a proposed
        patch commits neither the patch nor the reply."""
        from crank.models.job_search import JobSearchConversation

        class MidTurnDeleteWithPatchGateway:
            """Deletes the conversation mid-turn and proposes a patch."""

            def complete(self, request):
                JobSearchConversation.objects.filter(pk=conversation_pk).delete()
                from crank.agents.job_search.gateway import GatewayResponse

                return GatewayResponse(
                    text=json.dumps(
                        {
                            "message": "Noted your preference.",
                            "cited_organization_ids": [],
                            "cited_job_listing_ids": [],
                            "preference_patch": {"set": {"notes": "late patch"}},
                        }
                    )
                )

            def close(self):
                return None

        conversation_pk = self._start_conversation()
        provider = self._build_orchestrator_provider(
            MidTurnDeleteWithPatchGateway()
        )
        with self._patch_service_provider(provider):
            resp = self._submit(conversation_pk, "prefer remote", str(uuid.uuid4()))

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        stored = UserPreference.objects.get(user=self.user)
        self.assertEqual(stored.preferences["notes"], "")
        self.assertFalse(JobSearchMessage.objects.filter(role="assistant").exists())
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "conversation_closed" for c in calls))

    @patch("crank.views.job_search.monitoring.record_event")
    def test_preference_baseline_capture_failure_fails_closed(self, record):
        """MAJOR-4 (view level): when the baseline cannot be captured at turn
        start, a proposed patch aborts with the stable retryable 409 — the
        stale check is never silently skipped for a writer port."""
        from crank.agents.job_search.providers import OrchestratorJobSearchProvider

        class PatchProposingGateway:
            def complete(self, request):
                from crank.agents.job_search.gateway import GatewayResponse

                return GatewayResponse(
                    text=json.dumps(
                        {
                            "message": "Noted your preference.",
                            "cited_organization_ids": [],
                            "cited_job_listing_ids": [],
                            "preference_patch": {"set": {"notes": "unverified patch"}},
                        }
                    )
                )

            def close(self):
                return None

        conversation_pk = self._start_conversation()
        provider = self._build_orchestrator_provider(PatchProposingGateway())
        with self._patch_service_provider(provider):
            with patch.object(
                OrchestratorJobSearchProvider,
                "_read_preference_snapshot",
                return_value=("", None),
            ):
                resp = self._submit(conversation_pk, "prefer remote", str(uuid.uuid4()))

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "preference_stale")
        stored = UserPreference.objects.get(user=self.user)
        self.assertEqual(stored.preferences["notes"], "")
        conv = JobSearchConversation.objects.get(pk=conversation_pk)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 0)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(
            any(c.args[1].get("reason_code") == "preference_version_unavailable" for c in calls)
        )

    @patch("crank.views.job_search.monitoring.record_event")
    def test_reset_landing_between_recheck_and_insert_discards_reply(self, record):
        """MAJOR-1 window: a reset landing AFTER the locked re-check but BEFORE
        the assistant insert still cannot receive the reply.

        The hook flips the row inactive at the exact moment the insert runs
        (inside the persistence transaction); the in-transaction re-verify sees
        it, the whole transaction rolls back — including the simulated flip,
        because it shares the same transaction here — and the reply is
        discarded with a stable 409. On locking backends the flip cannot even
        interleave: reset/delete block on the conversation row lock until the
        insert commits.
        """

        class StaticProvider:
            def generate_reply(self, *, conversation, user_message):
                return "late reply", True, None

        conversation_pk = self._start_conversation()
        real_get_or_create = JobSearchMessage.objects.get_or_create

        def hooked_get_or_create(*args, **kwargs):
            if kwargs.get("role") == JobSearchMessage.Role.ASSISTANT:
                JobSearchConversation.objects.filter(pk=conversation_pk).update(
                    active=False
                )
            return real_get_or_create(*args, **kwargs)

        with self._patch_service_provider(StaticProvider()):
            with patch.object(JobSearchMessage.objects, "get_or_create", hooked_get_or_create):
                resp = self._submit(conversation_pk, "hello", str(uuid.uuid4()))

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        # The assistant insert was rolled back with the transaction.
        self.assertFalse(JobSearchMessage.objects.filter(role="assistant").exists())
        conv = JobSearchConversation.objects.get(pk=conversation_pk)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        # The simulated in-window flip shared the persistence transaction, so
        # it rolled back too — proving the insert and the flip were one
        # transactional step, not a read-then-write pair.
        self.assertTrue(conv.active)
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "conversation_closed" for c in calls))

    @patch("crank.views.job_search.monitoring.record_event")
    def test_delete_landing_between_recheck_and_insert_discards_reply(self, record):
        """MAJOR-1 window (delete): a delete landing after the locked re-check
        but before the insert makes the FK insert fail; nothing persists and
        the reply is discarded with a stable 409."""

        class StaticProvider:
            def generate_reply(self, *, conversation, user_message):
                return "late reply", True, None

        conversation_pk = self._start_conversation()
        real_get_or_create = JobSearchMessage.objects.get_or_create

        def hooked_get_or_create(*args, **kwargs):
            if kwargs.get("role") == JobSearchMessage.Role.ASSISTANT:
                JobSearchConversation.objects.filter(pk=conversation_pk).delete()
            return real_get_or_create(*args, **kwargs)

        with self._patch_service_provider(StaticProvider()):
            with patch.object(JobSearchMessage.objects, "get_or_create", hooked_get_or_create):
                resp = self._submit(conversation_pk, "hello", str(uuid.uuid4()))

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        # The insert (and the simulated in-window delete) rolled back; no
        # assistant message survived anywhere.
        self.assertFalse(JobSearchMessage.objects.filter(role="assistant").exists())
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(any(c.args[1].get("reason_code") == "conversation_closed" for c in calls))


@override_settings(CACHES=LOCMEM)
class GuardedTurnCommitTests(TestCase):
    """Issue #487 review round 2 (MAJOR-1/MAJOR-2), hook path.

    With the real orchestrator wired to the real preference store, the
    lifecycle guard's write-first row claim, the preference-patch write, and
    the reply persistence run in ONE transaction with a single commit
    boundary; backend lock contention maps to the retryable 409 envelopes.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("guarded", "guarded@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)
        preferences.read(self.user)

    def tearDown(self):
        cache.clear()

    def _start_conversation(self):
        resp = self.client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def _submit(self, conversation_id, content, key):
        return self.client.post(
            reverse("agent-conversation-detail", args=[conversation_id]),
            data=json.dumps({"content": content, "idempotency_key": key}),
            content_type="application/json",
        )

    def _patch_gateway_provider(self, gateway):
        from crank.agents.job_search.providers import (
            OrchestratorJobSearchProvider,
            _PreferenceServiceAdapter,
        )
        from crank.agents.job_search.service import JobSearchOrchestrator

        orchestrator = JobSearchOrchestrator(
            gateway=gateway,
            preference_service=_PreferenceServiceAdapter(self.user),
            org_datasource=lambda filters, limit: [],
            score_datasource=lambda ids, types, limit: [],
            job_listing_datasource=lambda filters, limit: [],
        )
        provider = OrchestratorJobSearchProvider(orchestrator=orchestrator)
        return patch.object(
            JobSearchService,
            "__init__",
            lambda self, *args, **kwargs: setattr(self, "provider", provider),
        )

    @staticmethod
    def _patch_gateway(payload):
        from crank.agents.job_search.gateway import GatewayResponse

        class PatchProposingGateway:
            def complete(self, request):
                return GatewayResponse(text=json.dumps(payload))

            def close(self):
                return None

        return PatchProposingGateway()

    @patch("crank.views.job_search.monitoring.record_event")
    def test_hook_path_patch_and_reply_roll_back_together(self, record):
        """MAJOR-1 (hook path): the patch write and the reply insert share one
        transaction — an in-window lifecycle flip rolls BOTH back."""
        conversation_pk = self._start_conversation()
        real_get_or_create = JobSearchMessage.objects.get_or_create

        def hooked_get_or_create(*args, **kwargs):
            if kwargs.get("role") == JobSearchMessage.Role.ASSISTANT:
                JobSearchConversation.objects.filter(pk=conversation_pk).update(
                    active=False
                )
            return real_get_or_create(*args, **kwargs)

        gateway = self._patch_gateway(
            {
                "message": "Noted your preference.",
                "cited_organization_ids": [],
                "cited_job_listing_ids": [],
                "preference_patch": {"set": {"notes": "late patch"}},
            }
        )
        with self._patch_gateway_provider(gateway):
            with patch.object(
                JobSearchMessage.objects, "get_or_create", hooked_get_or_create
            ):
                resp = self._submit(
                    conversation_pk, "prefer remote", str(uuid.uuid4())
                )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        # The patch write and the assistant insert were ONE transactional
        # step: the in-window flip rolled both back with the transaction.
        stored = UserPreference.objects.get(user=self.user)
        self.assertEqual(stored.preferences["notes"], "")
        conv = JobSearchConversation.objects.get(pk=conversation_pk)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 0)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        # The simulated flip shared the transaction, so it rolled back too.
        self.assertTrue(conv.active)

    @patch("crank.views.job_search.monitoring.record_event")
    def test_database_locked_on_patch_write_maps_to_retryable_409(self, record):
        """MAJOR-2: lock contention during the preference write surfaces as
        the retryable 409 ``preference_stale`` envelope, never a 500."""
        from django.db import OperationalError

        conversation_pk = self._start_conversation()
        gateway = self._patch_gateway(
            {
                "message": "Noted your preference.",
                "cited_organization_ids": [],
                "cited_job_listing_ids": [],
                "preference_patch": {"set": {"notes": "contended patch"}},
            }
        )
        with self._patch_gateway_provider(gateway):
            with patch(
                "crank.services.preferences.apply_patch_to_user",
                side_effect=OperationalError("database is locked"),
            ):
                resp = self._submit(
                    conversation_pk, "prefer remote", str(uuid.uuid4())
                )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "preference_stale")
        stored = UserPreference.objects.get(user=self.user)
        self.assertEqual(stored.preferences["notes"], "")
        conv = JobSearchConversation.objects.get(pk=conversation_pk)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 0)
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(
            any(c.args[1].get("reason_code") == "preference_stale" for c in calls)
        )

    @patch("crank.views.job_search.monitoring.record_event")
    def test_database_locked_on_lifecycle_claim_maps_to_retryable_409(self, record):
        """MAJOR-2: lock contention during the lifecycle guard claim surfaces
        as the retryable 409 ``conversation_closed`` envelope, never a 500."""
        from django.db import OperationalError

        from crank.agents.job_search.errors import ConversationClosedError
        from crank.agents.job_search.providers import OrchestratorJobSearchProvider

        def contended_guard():
            raise OperationalError("database is locked")

        conversation_pk = self._start_conversation()
        gateway = self._patch_gateway(
            {
                "message": "Noted your preference.",
                "cited_organization_ids": [],
                "cited_job_listing_ids": [],
                "preference_patch": {"set": {"notes": "contended patch"}},
            }
        )
        with self._patch_gateway_provider(gateway):
            with patch.object(
                OrchestratorJobSearchProvider,
                "_make_lifecycle_guard",
                return_value=contended_guard,
            ):
                resp = self._submit(
                    conversation_pk, "prefer remote", str(uuid.uuid4())
                )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        stored = UserPreference.objects.get(user=self.user)
        self.assertEqual(stored.preferences["notes"], "")
        conv = JobSearchConversation.objects.get(pk=conversation_pk)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 0)
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(
            any(
                c.args[1].get("reason_code") == "conversation_closed" for c in calls
            )
        )

    @patch("crank.views.job_search.monitoring.record_event")
    def test_non_lock_operational_error_is_not_translated(self, record):
        """Review round 3 MAJOR (narrowness / MySQL probe): a NON-contention
        backend failure (e.g. a MySQL connection error) is never translated
        into the retryable 409 envelopes — it keeps the existing stable 500
        ``invalid_output`` path. The outer-commit translation shares the same
        narrow ``is_database_locked`` predicate as the inner guarded blocks,
        so the mapping cannot mask genuine backend faults as "retry"."""
        from django.db import OperationalError

        conversation_pk = self._start_conversation()
        gateway = self._patch_gateway(
            {
                "message": "Noted your preference.",
                "cited_organization_ids": [],
                "cited_job_listing_ids": [],
                "preference_patch": {"set": {"notes": "unrelated failure"}},
            }
        )
        with self._patch_gateway_provider(gateway):
            with patch(
                "crank.services.preferences.apply_patch_to_user",
                side_effect=OperationalError("connection refused"),
            ):
                resp = self._submit(
                    conversation_pk, "prefer remote", str(uuid.uuid4())
                )

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["error"]["type"], "invalid_output")
        stored = UserPreference.objects.get(user=self.user)
        self.assertEqual(stored.preferences["notes"], "")
        conv = JobSearchConversation.objects.get(pk=conversation_pk)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 0)
        calls = [c for c in record.call_args_list if c.args[0] == "interactive_call"]
        self.assertTrue(
            any(c.args[1].get("reason_code") == "invalid_output" for c in calls)
        )

    @override_settings(JOB_SEARCH_RESPONSE_MAX_LEN=10)
    @patch("crank.views.job_search.monitoring.record_event")
    def test_hook_path_reply_length_bound_applied_before_persistence(self, record):
        """Review round 3 MINOR: the transport's reply-length bound is applied
        BEFORE the persist_reply hook persists the message — the persisted AND
        returned message respect ``JOB_SEARCH_RESPONSE_MAX_LEN`` on the
        production orchestrator hook path. Previously the hook persisted the
        unbounded orchestrator message and the view responded with it, so a
        15-char reply with ``MAX_LEN=10`` persisted all 15 chars."""
        conversation_pk = self._start_conversation()
        gateway = self._patch_gateway(
            {
                "message": "123456789012345",  # 15 chars, over the bound of 10
                "cited_organization_ids": [],
                "cited_job_listing_ids": [],
                "preference_patch": None,
            }
        )
        with self._patch_gateway_provider(gateway):
            resp = self._submit(conversation_pk, "prefer remote", str(uuid.uuid4()))

        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(len(body["message"]["content"]), 10)
        conv = JobSearchConversation.objects.get(pk=conversation_pk)
        assistant = conv.messages.get(role="assistant")
        self.assertEqual(len(assistant.content), 10)
        self.assertEqual(assistant.content, body["message"]["content"])

    def test_retry_after_midturn_reset_recovers_retained_turn(self):
        """MINOR (retryability): after a mid-turn reset the retained user turn
        is recoverable — the same content + idempotency key replays onto the
        new active conversation and succeeds."""
        from crank.models.job_search import JobSearchConversation

        class MidTurnResetProvider:
            def generate_reply(self, *, conversation, user_message):
                JobSearchConversation.objects.filter(pk=conversation.pk).update(
                    active=False
                )
                JobSearchConversation.objects.create(owner=conversation.owner)
                return "recovered reply", False, None

        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with patch.object(
            JobSearchService,
            "__init__",
            lambda self, *args, **kwargs: setattr(
                self, "provider", MidTurnResetProvider()
            ),
        ):
            resp = self._submit(conv_id, "hello there", key)

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        old = JobSearchConversation.objects.get(pk=conv_id)
        self.assertFalse(old.active)
        # The retained user turn survives on the closed conversation.
        self.assertEqual(old.messages.filter(role="user").count(), 1)

        # Recovery: switch to the user's active (fresh) conversation and
        # replay the retained turn with the SAME idempotency key.
        resume = self.client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resume.status_code, 200)
        new_id = resume.json()["id"]
        self.assertNotEqual(new_id, conv_id)
        retry = self._submit(new_id, "hello there", key)
        self.assertEqual(retry.status_code, 201)
        new_conv = JobSearchConversation.objects.get(pk=new_id)
        self.assertEqual(new_conv.messages.filter(role="user").count(), 1)
        self.assertEqual(
            new_conv.messages.filter(
                role="assistant", idempotency_key=key
            ).count(),
            1,
        )
        # The original retained row is untouched on the closed conversation.
        old = JobSearchConversation.objects.get(pk=conv_id)
        self.assertEqual(old.messages.filter(role="user", idempotency_key=key).count(), 1)
        self.assertEqual(old.messages.filter(role="assistant").count(), 0)

    def test_retry_after_midturn_delete_recreates_and_recovers_turn(self):
        """MINOR (retryability, delete variant): after a mid-turn delete the
        same content + idempotency key replays onto a freshly created active
        conversation."""
        from crank.models.job_search import JobSearchConversation

        class MidTurnDeleteProvider:
            def generate_reply(self, *, conversation, user_message):
                conversation.messages.all().delete()
                JobSearchConversation.objects.filter(pk=conversation.pk).delete()
                return "recovered reply", False, None

        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with patch.object(
            JobSearchService,
            "__init__",
            lambda self, *args, **kwargs: setattr(
                self, "provider", MidTurnDeleteProvider()
            ),
        ):
            resp = self._submit(conv_id, "hello there", key)

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        self.assertFalse(JobSearchConversation.objects.filter(pk=conv_id).exists())

        # Recovery: no active conversation remains, so the resume call
        # creates a fresh one; the retained turn replays with the same key.
        resume = self.client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resume.status_code, 201)
        new_id = resume.json()["id"]
        retry = self._submit(new_id, "hello there", key)
        self.assertEqual(retry.status_code, 201)
        new_conv = JobSearchConversation.objects.get(pk=new_id)
        self.assertEqual(
            new_conv.messages.filter(role="assistant", idempotency_key=key).count(),
            1,
        )




class GuardedTurnCommitConcurrencyTests(TransactionTestCase):
    """Reviewer repro (PR #501 fix-verification round 2) as regression tests.

    Two real database connections (file-backed SQLite via ``TransactionTestCase``)
    race a guarded turn against a conversation reset:

    * MAJOR-2 probe — a reset landing while the turn holds the conversation
      row's write lock (after the guard claim, before the patch write) cannot
      make the turn fail with ``500 invalid_output``; the turn completes and
      the reset's bounded retry lands it after the single commit.
    * MAJOR-1 repro — a reset landing after the patch write but before the
      single commit leaves NO committed patch on a closed conversation: the
      reset blocks until the turn's commit, then closes the conversation.
    * A reset fully completing before the guard claim aborts the whole turn
      with the retryable 409 and nothing persisted.
    """

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _setup_user_and_conversation(self, username):
        user = User.objects.create_user(username, f"{username}@example.com", "pw")
        preferences.read(user)
        client = Client()
        client.force_login(user)
        create = client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 201)
        return user, client, create.json()["id"]

    def _build_orchestrator_provider(self, user, gateway, preference_service=None):
        from crank.agents.job_search.providers import (
            OrchestratorJobSearchProvider,
            _PreferenceServiceAdapter,
        )
        from crank.agents.job_search.service import JobSearchOrchestrator

        orchestrator = JobSearchOrchestrator(
            gateway=gateway,
            preference_service=preference_service or _PreferenceServiceAdapter(user),
            org_datasource=lambda filters, limit: [],
            score_datasource=lambda ids, types, limit: [],
            job_listing_datasource=lambda filters, limit: [],
        )
        return OrchestratorJobSearchProvider(orchestrator=orchestrator)

    @staticmethod
    def _patch_provider(provider):
        return patch.object(
            JobSearchService,
            "__init__",
            lambda self, *args, **kwargs: setattr(self, "provider", provider),
        )

    @staticmethod
    def _patch_gateway(payload):
        from crank.agents.job_search.gateway import GatewayResponse

        class PatchProposingGateway:
            def complete(self, request):
                return GatewayResponse(text=json.dumps(payload))

            def close(self):
                return None

        return PatchProposingGateway()

    def _race_turn_against_reset(self, user, provider, conv_id, pause_in_pref_write):
        """Run the turn in one connection, pause it inside the guarded commit
        window, race a reset from a second client, then resume the turn."""
        from django.db import connections

        reached = threading.Event()
        resume = threading.Event()
        responses = {}
        key = str(uuid.uuid4())
        patch_payload = {
            "message": "Noted your preference.",
            "cited_organization_ids": [],
            "cited_job_listing_ids": [],
            "preference_patch": {"set": {"notes": "late patch"}},
        }
        cleanup = []

        if pause_in_pref_write:
            # MAJOR-2 probe pause point: after the guard claim, before the
            # preference write.
            from crank.agents.job_search.providers import _PreferenceServiceAdapter

            class PausingPreferenceService:
                writable = True

                def __init__(self, inner):
                    self._inner = inner

                def validate_patch(self, patch):
                    self._inner.validate_patch(patch)

                def apply_patch(self, patch, expected_modified=None):
                    reached.set()
                    if not resume.wait(timeout=20):
                        raise RuntimeError("turn was never resumed")
                    return self._inner.apply_patch(
                        patch, expected_modified=expected_modified
                    )

            preference_service = PausingPreferenceService(
                _PreferenceServiceAdapter(user)
            )
            provider = self._build_orchestrator_provider(
                user, self._patch_gateway(patch_payload), preference_service
            )
        else:
            # MAJOR-1 repro pause point: after the patch write, before the
            # reply insert / single commit.
            real_get_or_create = JobSearchMessage.objects.get_or_create

            def paused_get_or_create(*args, **kwargs):
                if kwargs.get("role") == JobSearchMessage.Role.ASSISTANT:
                    reached.set()
                    if not resume.wait(timeout=20):
                        raise RuntimeError("turn was never resumed")
                return real_get_or_create(*args, **kwargs)

            cleanup.append(
                patch.object(
                    JobSearchMessage.objects, "get_or_create", paused_get_or_create
                )
            )
            provider = self._build_orchestrator_provider(
                user, self._patch_gateway(patch_payload)
            )

        def run_turn():
            try:
                turn_client = Client()
                turn_client.force_login(user)
                with self._patch_provider(provider):
                    responses["turn"] = turn_client.post(
                        reverse("agent-conversation-detail", args=[conv_id]),
                        data=json.dumps({"content": "prefer remote", "idempotency_key": key}),
                        content_type="application/json",
                    )
            finally:
                connections.close_all()

        def run_reset():
            try:
                reset_client = Client()
                reset_client.force_login(user)
                responses["reset"] = reset_client.post(
                    reverse("agent-conversation-reset", args=[conv_id])
                )
            finally:
                connections.close_all()

        turn_thread = threading.Thread(target=run_turn)
        for cm in cleanup:
            # Enter the pause hooks BEFORE the turn starts: the turn thread
            # must hit the patched hook for ``reached`` to ever fire.
            cm.__enter__()
        turn_thread.start()
        self.assertTrue(reached.wait(timeout=20), "turn never reached pause point")
        reset_thread = threading.Thread(target=run_reset)
        reset_thread.start()
        time.sleep(0.3)  # let the reset block on the turn's row write lock
        resume.set()
        turn_thread.join(timeout=30)
        reset_thread.join(timeout=30)
        self.assertFalse(turn_thread.is_alive())
        self.assertFalse(reset_thread.is_alive())
        for cm in cleanup:
            cm.__exit__(None, None, None)
        return responses

    def test_reset_landing_between_claim_and_patch_write_lands_after_commit(self):
        """MAJOR-2 two-connection probe: a reset racing the guarded window
        after the guard claim cannot turn the reply into ``500 invalid_output``;
        the turn completes (patch + reply in one commit) and the reset's
        bounded retry lands it after that commit."""
        user, client, conv_id = self._setup_user_and_conversation("probe1")
        provider = self._build_orchestrator_provider(user, self._patch_gateway({}))

        responses = self._race_turn_against_reset(
            user, provider, conv_id, pause_in_pref_write=True
        )

        self.assertEqual(responses["turn"].status_code, 201)
        self.assertEqual(responses["reset"].status_code, 201)
        # The patch and the reply committed together inside the guarded
        # transaction — no 500, no lost patch.
        stored = UserPreference.objects.get(user=user)
        self.assertEqual(stored.preferences["notes"], "late patch")
        old = JobSearchConversation.objects.get(pk=conv_id)
        self.assertEqual(old.messages.filter(role="assistant").count(), 1)
        # The reset then closed the conversation and created a fresh one.
        self.assertFalse(old.active)
        fresh = JobSearchConversation.objects.filter(owner=user, active=True).first()
        self.assertIsNotNone(fresh)
        self.assertNotEqual(fresh.pk, conv_id)

    def test_reset_landing_after_patch_write_blocks_until_single_commit(self):
        """MAJOR-1 (reviewer repro): a reset landing after the patch write but
        before the single commit blocks until the commit — the patch and the
        reply persist together on the still-active conversation, and the reset
        closes it afterwards. The old post-patch/pre-reply window (patch
        committed, reply discarded with 409) is gone."""
        user, client, conv_id = self._setup_user_and_conversation("probe2")

        responses = self._race_turn_against_reset(
            user, None, conv_id, pause_in_pref_write=False
        )

        self.assertEqual(responses["turn"].status_code, 201)
        self.assertEqual(responses["reset"].status_code, 201)
        stored = UserPreference.objects.get(user=user)
        self.assertEqual(stored.preferences["notes"], "late patch")
        old = JobSearchConversation.objects.get(pk=conv_id)
        self.assertEqual(old.messages.filter(role="assistant").count(), 1)
        self.assertFalse(old.active)
        fresh = JobSearchConversation.objects.filter(owner=user, active=True).first()
        self.assertIsNotNone(fresh)
        self.assertNotEqual(fresh.pk, conv_id)

    def test_reset_completing_before_guard_claim_yields_retryable_409(self):
        """A reset fully completing before the guard claim aborts the whole
        turn: 409 ``conversation_closed``, neither patch nor reply persisted."""
        from django.db import connections

        user, client, conv_id = self._setup_user_and_conversation("probe3")
        reached = threading.Event()
        resume = threading.Event()
        responses = {}
        gateway_payload = {
            "message": "Noted your preference.",
            "cited_organization_ids": [],
            "cited_job_listing_ids": [],
            "preference_patch": {"set": {"notes": "late patch"}},
        }

        class PausingGateway:
            def complete(self, request):
                reached.set()
                if not resume.wait(timeout=20):
                    raise RuntimeError("turn was never resumed")
                return self.__class__._response(gateway_payload)

            @staticmethod
            def _response(payload):
                from crank.agents.job_search.gateway import GatewayResponse

                return GatewayResponse(text=json.dumps(payload))

            def close(self):
                return None

        provider = self._build_orchestrator_provider(user, PausingGateway())
        key = str(uuid.uuid4())

        def run_turn():
            try:
                turn_client = Client()
                turn_client.force_login(user)
                with self._patch_provider(provider):
                    responses["turn"] = turn_client.post(
                        reverse("agent-conversation-detail", args=[conv_id]),
                        data=json.dumps({"content": "prefer remote", "idempotency_key": key}),
                        content_type="application/json",
                    )
            finally:
                connections.close_all()

        def run_reset():
            try:
                reset_client = Client()
                reset_client.force_login(user)
                responses["reset"] = reset_client.post(
                    reverse("agent-conversation-reset", args=[conv_id])
                )
            finally:
                connections.close_all()

        turn_thread = threading.Thread(target=run_turn)
        turn_thread.start()
        self.assertTrue(reached.wait(timeout=20), "turn never reached the gateway")
        # The turn holds NO database transaction here (the guarded window has
        # not started), so the reset completes immediately.
        reset_thread = threading.Thread(target=run_reset)
        reset_thread.start()
        reset_thread.join(timeout=20)
        resume.set()
        turn_thread.join(timeout=30)
        self.assertFalse(turn_thread.is_alive())

        self.assertEqual(responses["reset"].status_code, 201)
        self.assertEqual(responses["turn"].status_code, 409)
        self.assertEqual(responses["turn"].json()["error"]["type"], "conversation_closed")
        stored = UserPreference.objects.get(user=user)
        self.assertEqual(stored.preferences["notes"], "")
        old = JobSearchConversation.objects.get(pk=conv_id)
        self.assertFalse(old.active)
        self.assertEqual(old.messages.filter(role="assistant").count(), 0)
        self.assertEqual(old.messages.filter(role="user").count(), 1)

    @patch("crank.views.job_search.monitoring.record_event")
    def test_outer_commit_lock_contention_maps_to_retryable_409(self, record):
        """Review round 3 MAJOR: lock contention raised by the guarded
        transaction's own COMMIT — the outer boundary, AFTER every inner
        guarded block has run and been mapped — maps to the retryable 409
        ``conversation_closed`` envelope, never ``500 invalid_output``.

        Reviewer repro pattern (two real SQLite connections): connection B
        holds a read transaction (a SHARED lock); connection A (the turn)
        claims the conversation row and writes patch + reply (RESERVED),
        then A's COMMIT needs the EXCLUSIVE lock B is blocking and fails
        with ``database is locked`` at the outer commit."""
        from django.db import connections, transaction

        user, client, conv_id = self._setup_user_and_conversation("probe4")
        reached = threading.Event()
        resume = threading.Event()
        responses = {}
        key = str(uuid.uuid4())
        patch_payload = {
            "message": "Noted your preference.",
            "cited_organization_ids": [],
            "cited_job_listing_ids": [],
            "preference_patch": {"set": {"notes": "commit-contended patch"}},
        }
        real_get_or_create = JobSearchMessage.objects.get_or_create

        def paused_get_or_create(*args, **kwargs):
            # Pause AFTER the guard claim + patch write (A holds RESERVED),
            # before the reply insert: the outer commit still lies ahead.
            if kwargs.get("role") == JobSearchMessage.Role.ASSISTANT:
                reached.set()
                if not resume.wait(timeout=30):
                    raise RuntimeError("turn was never resumed")
            return real_get_or_create(*args, **kwargs)

        provider = self._build_orchestrator_provider(
            user, self._patch_gateway(patch_payload)
        )

        def run_turn():
            try:
                turn_client = Client()
                turn_client.force_login(user)
                with patch.object(
                    JobSearchMessage.objects, "get_or_create", paused_get_or_create
                ):
                    with self._patch_provider(provider):
                        responses["turn"] = turn_client.post(
                            reverse("agent-conversation-detail", args=[conv_id]),
                            data=json.dumps(
                                {"content": "prefer remote", "idempotency_key": key}
                            ),
                            content_type="application/json",
                        )
            finally:
                connections.close_all()

        turn_thread = threading.Thread(target=run_turn)
        turn_thread.start()
        self.assertTrue(reached.wait(timeout=20), "turn never reached pause point")

        # Connection B (this thread): hold a read transaction — a SHARED
        # lock — while A holds RESERVED. SHARED is compatible with RESERVED,
        # so B acquires it; A's COMMIT then needs EXCLUSIVE and cannot get
        # it while B's read transaction stays open.
        holder = transaction.atomic()
        holder.__enter__()
        list(JobSearchConversation.objects.filter(pk=conv_id))
        try:
            resume.set()
            turn_thread.join(timeout=60)
            self.assertFalse(turn_thread.is_alive())
            resp = responses["turn"]
            # The outer-commit contention surfaced as the retryable 409,
            # not the 500 ``invalid_output`` broad handler.
            self.assertEqual(resp.status_code, 409)
            self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
            calls = [
                c for c in record.call_args_list if c.args[0] == "interactive_call"
            ]
            self.assertTrue(
                any(
                    c.args[1].get("reason_code") == "conversation_closed"
                    for c in calls
                )
            )
        finally:
            holder.__exit__(None, None, None)

        # The failed commit rolled the whole guarded transaction back: no
        # patch, no reply; the earlier-committed user turn remains retryable
        # with the same idempotency key.
        stored = UserPreference.objects.get(user=user)
        self.assertEqual(stored.preferences["notes"], "")
        conv = JobSearchConversation.objects.get(pk=conv_id)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 0)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        self.assertTrue(conv.active)


@override_settings(CACHES=LOCMEM)
class TurnDeliveryStateTests(TestCase):
    """Turn delivery state machine and serialization (issue #458)."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("turnstate", "ts@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()

    def _start_conversation(self):
        resp = self.client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def _submit(self, conversation_id, content, key):
        return self.client.post(
            reverse("agent-conversation-detail", args=[conversation_id]),
            data=json.dumps({"content": content, "idempotency_key": key}),
            content_type="application/json",
        )

    def test_new_user_message_persists_pending_then_completed(self):
        """A first submission records the completed delivery outcome on the
        turn anchor; the serialized user message exposes its key and state."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        resp = self._submit(conv_id, "durable turn", key)
        self.assertEqual(resp.status_code, 201)
        turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.COMPLETED)
        self.assertEqual(turn.failure_code, "")
        self.assertEqual(turn.attempt_count, 1)
        self.assertIsNone(turn.lease_expires_at)

        # The user message serialization exposes the key and delivery state.
        history = self.client.get(
            reverse("agent-conversation-detail", args=[conv_id])
        ).json()
        user_entries = [m for m in history["messages"] if m["role"] == "user"]
        self.assertEqual(len(user_entries), 1)
        self.assertEqual(user_entries[0]["idempotency_key"], key)
        self.assertEqual(user_entries[0]["delivery_state"], "completed")
        self.assertFalse(user_entries[0]["retry_available"])
        # Assistant messages stay unchanged: no turn-state fields.
        assistant_entries = [
            m for m in history["messages"] if m["role"] == "assistant"
        ]
        self.assertEqual(len(assistant_entries), 1)
        self.assertNotIn("idempotency_key", assistant_entries[0])
        self.assertNotIn("delivery_state", assistant_entries[0])
        self.assertNotIn("retry_available", assistant_entries[0])

    def test_failed_turn_records_state_and_failure_code(self):
        """A provider failure persists delivery_state=failed plus the stable
        failure code, and the GET surfaces both to the client."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with patch.object(
            JobSearchService, "run_turn",
            side_effect=ServiceTimeout("timed out"),
        ):
            resp = self._submit(conv_id, "fails", key)
        self.assertEqual(resp.status_code, 504)
        turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.FAILED)
        self.assertEqual(
            turn.failure_code, JobSearchTurn.FailureCode.PROVIDER_TIMEOUT
        )
        self.assertEqual(turn.attempt_count, 1)

        # A reloaded client sees the failed turn and can retry it by key.
        history = self.client.get(
            reverse("agent-conversation-detail", args=[conv_id])
        ).json()
        user_entries = [m for m in history["messages"] if m["role"] == "user"]
        self.assertEqual(user_entries[0]["delivery_state"], "failed")
        self.assertEqual(user_entries[0]["idempotency_key"], key)
        self.assertTrue(user_entries[0]["retry_available"])

        # Retry with the same key recovers: one assistant reply, completed.
        real_run = JobSearchService().run_turn.__func__
        with patch.object(
            JobSearchService, "run_turn", autospec=True, side_effect=real_run
        ):
            retry = self._submit(conv_id, "fails", key)
        self.assertEqual(retry.status_code, 201)
        conv = JobSearchConversation.objects.get(pk=conv_id)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 1)
        turn.refresh_from_db()
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.COMPLETED)
        self.assertEqual(turn.failure_code, "")
        self.assertEqual(turn.attempt_count, 2)

    def test_every_failure_category_persists_its_failure_code(self):
        """Each mapped service exception records its matching failure_code."""
        cases = [
            (AssistantUnavailable("down"), JobSearchTurn.FailureCode.ASSISTANT_UNAVAILABLE),
            (ServiceTimeout("slow"), JobSearchTurn.FailureCode.PROVIDER_TIMEOUT),
            (ServiceCostLimit("budget"), JobSearchTurn.FailureCode.COST_LIMIT),
            (ServiceInvalidOutput("bad"), JobSearchTurn.FailureCode.INVALID_OUTPUT),
            (JobSearchServiceError("boom"), JobSearchTurn.FailureCode.SERVICE_ERROR),
        ]
        for i, (exc, expected_code) in enumerate(cases):
            conv_id = self._start_conversation()
            key = str(uuid.uuid4())
            with patch.object(JobSearchService, "run_turn", side_effect=exc):
                resp = self._submit(conv_id, "turn {}".format(i), key)
            self.assertIn(resp.status_code, (429, 500, 503, 504))
            turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
            self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.FAILED)
            self.assertEqual(turn.failure_code, expected_code)

    def test_pending_turn_returns_409_without_provider_call(self):
        """A turn left pending (in flight elsewhere) gets a stable 409 with no
        provider call and no preference application."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        # Simulate another request holding the turn: a live pending claim
        # (lease still valid) plus its persisted user message.
        JobSearchMessage.objects.create(
            conversation_id=conv_id,
            role="user",
            content="in flight",
            idempotency_key=key,
        )
        JobSearchTurn.objects.create(
            conversation_id=conv_id,
            turn_key=key,
            delivery_state=JobSearchTurn.DeliveryState.PENDING,
            attempt_count=1,
            lease_expires_at=timezone.now() + timedelta(seconds=300),
        )
        run_calls = {"n": 0}

        def counting_run_turn(*args, **kwargs):
            run_calls["n"] += 1
            return JobSearchService().run_turn(*args, **kwargs)

        with patch.object(
            JobSearchService, "run_turn", autospec=True, side_effect=counting_run_turn
        ):
            resp = self._submit(conv_id, "in flight", key)
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "turn_in_progress")
        self.assertTrue(body["error"].get("request_id"))
        # No provider call happened, and the live claim is untouched.
        self.assertEqual(run_calls["n"], 0)
        turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.PENDING)
        self.assertEqual(turn.failure_code, "")
        self.assertEqual(turn.attempt_count, 1)
        self.assertEqual(
            JobSearchMessage.objects.filter(
                conversation_id=conv_id, role="assistant"
            ).count(),
            0,
        )

    def test_pending_turn_via_get_or_create_race_returns_409(self):
        """Losing the anchor create race against a pending same-key turn 409s
        instead of re-running the provider."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())

        real_get_or_create = JobSearchTurn.objects.get_or_create

        def racing_get_or_create(*args, **kwargs):
            # The concurrent winner claimed the anchor (pending, live lease)
            # between our existence check and our create.
            real_get_or_create(
                conversation_id=conv_id,
                turn_key=key,
                defaults={
                    "delivery_state": JobSearchTurn.DeliveryState.PENDING,
                    "attempt_count": 1,
                    "lease_expires_at": timezone.now() + timedelta(seconds=300),
                },
            )
            return real_get_or_create(*args, **kwargs)

        with patch.object(
            JobSearchTurn.objects, "get_or_create", side_effect=racing_get_or_create
        ):
            resp = self._submit(conv_id, "racer", key)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "turn_in_progress")
        self.assertEqual(
            JobSearchMessage.objects.filter(
                conversation_id=conv_id, role="assistant"
            ).count(),
            0,
        )
        # The anchor row is unique: the loser adopted the winner's row.
        self.assertEqual(
            JobSearchTurn.objects.filter(conversation_id=conv_id).count(), 1
        )

    def test_legacy_rows_derive_delivery_state_at_read_time(self):
        """Rows persisted before delivery_state existed (empty value) derive
        completed/failed from the presence of a matching assistant reply."""
        conv_id = self._start_conversation()
        answered_key = str(uuid.uuid4())
        unanswered_key = str(uuid.uuid4())
        keyless_row = JobSearchMessage.objects.create(
            conversation_id=conv_id, role="user", content="pre-key era"
        )
        JobSearchMessage.objects.create(
            conversation_id=conv_id,
            role="user",
            content="answered legacy",
            idempotency_key=answered_key,
        )
        JobSearchMessage.objects.create(
            conversation_id=conv_id,
            role="user",
            content="unanswered legacy",
            idempotency_key=unanswered_key,
        )
        JobSearchMessage.objects.create(
            conversation_id=conv_id,
            role="assistant",
            content="legacy reply",
            idempotency_key=answered_key,
        )
        history = self.client.get(
            reverse("agent-conversation-detail", args=[conv_id])
        ).json()
        by_content = {m["content"]: m for m in history["messages"] if m["role"] == "user"}
        # Empty-state rows with a matching assistant reply derive completed.
        self.assertEqual(by_content["answered legacy"]["delivery_state"], "completed")
        self.assertFalse(by_content["answered legacy"]["retry_available"])
        # Empty-state rows without one derive failed (retriable).
        self.assertEqual(by_content["unanswered legacy"]["delivery_state"], "failed")
        self.assertEqual(by_content["unanswered legacy"]["idempotency_key"], unanswered_key)
        self.assertTrue(by_content["unanswered legacy"]["retry_available"])
        # Pre-key rows have nothing retriable and report completed.
        self.assertEqual(by_content["pre-key era"]["delivery_state"], "completed")
        self.assertEqual(by_content["pre-key era"]["idempotency_key"], "")

    def test_failed_turn_retry_after_late_first_completion_replays(self):
        """A turn that already completed replays the stored reply instead of
        re-running the provider, even when a client believed it failed."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        first = self._submit(conv_id, "only once", key)
        self.assertEqual(first.status_code, 201)
        first_reply = first.json()["message"]["content"]

        # A second "session" (reloaded client) retries the same key after
        # believing the turn failed.
        self.client.logout()
        reloaded = Client()
        reloaded.force_login(self.user)
        replay = reloaded.post(
            reverse("agent-conversation-detail", args=[conv_id]),
            data=json.dumps({"content": "only once", "idempotency_key": key}),
            content_type="application/json",
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["message"]["content"], first_reply)
        conv = JobSearchConversation.objects.get(pk=conv_id)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 1)

    def test_retry_against_reset_or_deleted_conversation_404s(self):
        """Retrying a key from a reset (archived) or deleted conversation
        returns 404; no state leaks across conversations."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with patch.object(
            JobSearchService, "run_turn", side_effect=ServiceTimeout("slow")
        ):
            self._submit(conv_id, "orphan key", key)
        # Reset archives the conversation.
        reset = self.client.post(reverse("agent-conversation-reset", args=[conv_id]))
        self.assertEqual(reset.status_code, 201)
        new_id = reset.json()["id"]
        # Same key against the archived conversation: 404.
        orphan = self._submit(conv_id, "orphan key", key)
        self.assertEqual(orphan.status_code, 404)
        # Same key against a *different* (new) conversation: 404 because the
        # turn state is keyed per conversation, never global.
        cross = self._submit(new_id, "orphan key", key)
        self.assertEqual(cross.status_code, 201)
        self.assertEqual(
            JobSearchMessage.objects.filter(idempotency_key=key, role="user").count(),
            2,
        )
        # Delete the new conversation entirely; retry is a 404.
        delete = self.client.post(reverse("agent-conversation-delete", args=[new_id]))
        self.assertEqual(delete.status_code, 200)
        self.assertEqual(self._submit(new_id, "orphan key", key).status_code, 404)

    def test_rate_limited_new_key_does_not_persist_a_turn(self):
        """A rate-limited new key leaves no user row or anchor behind (the
        budget check happens before any persistence)."""
        conv_id = self._start_conversation()
        with override_settings(JOB_SEARCH_RATE_LIMIT_PER_HOUR=0):
            resp = self._submit(conv_id, "throttled", str(uuid.uuid4()))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            JobSearchMessage.objects.filter(conversation_id=conv_id).count(), 0
        )
        self.assertEqual(
            JobSearchTurn.objects.filter(conversation_id=conv_id).count(), 0
        )

    # -- attempt cap (adversarial review: unbounded poisoned retries) --------

    @override_settings(JOB_SEARCH_TURN_MAX_ATTEMPTS=2)
    def test_retry_limit_reached_after_attempt_cap(self):
        """Retries are bounded per turn: after the cap, the same key is
        rejected with a stable envelope that documents the cap, and no
        further provider call runs."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        run_calls = {"n": 0}

        def always_fails(*args, **kwargs):
            run_calls["n"] += 1
            raise ServiceInvalidOutput("bad output")

        with patch.object(
            JobSearchService, "run_turn", autospec=True, side_effect=always_fails
        ):
            first = self._submit(conv_id, "poisoned turn", key)
            self.assertEqual(first.status_code, 500)
            retry = self._submit(conv_id, "poisoned turn", key)
            self.assertEqual(retry.status_code, 500)
            # The third attempt is rejected at the cap — no provider call.
            capped = self._submit(conv_id, "poisoned turn", key)
            capped_again = self._submit(conv_id, "poisoned turn", key)
        self.assertEqual(capped.status_code, 429)
        self.assertEqual(capped.json()["error"]["type"], "retry_limit_reached")
        self.assertIn("2 attempts", capped.json()["error"]["message"])
        self.assertEqual(capped_again.status_code, 429)
        self.assertEqual(
            capped_again.json()["error"]["type"], "retry_limit_reached"
        )
        self.assertEqual(run_calls["n"], 2)
        turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
        self.assertEqual(turn.attempt_count, 2)
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.FAILED)
        # The GET tells the client the turn is not retriable anymore.
        history = self.client.get(
            reverse("agent-conversation-detail", args=[conv_id])
        ).json()
        user_entry = [m for m in history["messages"] if m["role"] == "user"][0]
        self.assertEqual(user_entry["delivery_state"], "failed")
        self.assertFalse(user_entry["retry_available"])

    @override_settings(JOB_SEARCH_TURN_MAX_ATTEMPTS=3)
    def test_retry_within_cap_succeeds_after_transient_failures(self):
        """A turn that fails transiently can be retried up to (not past) the
        cap; the successful retry marks the turn completed."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        real_run_turn = JobSearchService().run_turn.__func__
        failures = {"n": 0}

        def flaky(*args, **kwargs):
            failures["n"] += 1
            if failures["n"] <= 2:
                raise ServiceTimeout("slow")
            return real_run_turn(*args, **kwargs)

        with patch.object(
            JobSearchService, "run_turn", autospec=True, side_effect=flaky
        ):
            first = self._submit(conv_id, "eventually works", key)
            self.assertEqual(first.status_code, 504)
            second = self._submit(conv_id, "eventually works", key)
            self.assertEqual(second.status_code, 504)
            third = self._submit(conv_id, "eventually works", key)
        self.assertEqual(third.status_code, 201)
        turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.COMPLETED)
        self.assertEqual(turn.attempt_count, 3)
        history = self.client.get(
            reverse("agent-conversation-detail", args=[conv_id])
        ).json()
        user_entry = [m for m in history["messages"] if m["role"] == "user"][0]
        self.assertEqual(user_entry["delivery_state"], "completed")
        self.assertFalse(user_entry["retry_available"])

    # -- lease recovery (adversarial review: interrupted workers) -----------

    def _stale_claim(self, conv_id, key, attempt_count=1):
        JobSearchMessage.objects.create(
            conversation_id=conv_id,
            role="user",
            content="interrupted",
            idempotency_key=key,
        )
        JobSearchTurn.objects.create(
            conversation_id=conv_id,
            turn_key=key,
            delivery_state=JobSearchTurn.DeliveryState.PENDING,
            attempt_count=attempt_count,
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )

    def test_get_reaps_stale_pending_turns(self):
        """Recovery-on-read: a pending claim whose lease expired is marked
        failed-and-retryable, never stuck at turn_in_progress forever."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        self._stale_claim(conv_id, key)
        history = self.client.get(
            reverse("agent-conversation-detail", args=[conv_id])
        ).json()
        user_entry = [m for m in history["messages"] if m["role"] == "user"][0]
        self.assertEqual(user_entry["delivery_state"], "failed")
        self.assertTrue(user_entry["retry_available"])
        turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.FAILED)
        self.assertEqual(
            turn.failure_code, JobSearchTurn.FailureCode.WORKER_INTERRUPTED
        )
        self.assertIsNone(turn.lease_expires_at)

    def test_stale_pending_claim_is_taken_over_on_retry(self):
        """A retry against an expired claim takes the turn over and runs the
        provider, so an interrupted worker never strands the turn."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        self._stale_claim(conv_id, key)
        real_run_turn = JobSearchService().run_turn.__func__
        with patch.object(
            JobSearchService, "run_turn", autospec=True, side_effect=real_run_turn
        ):
            resp = self._submit(conv_id, "interrupted", key)
        self.assertEqual(resp.status_code, 201)
        turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.COMPLETED)
        self.assertEqual(turn.attempt_count, 2)
        conv = JobSearchConversation.objects.get(pk=conv_id)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 1)

    def test_live_pending_claim_is_not_reaped(self):
        """A pending claim with a valid lease serializes as pending on GET
        and a same-key retry still gets the stable 409."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        JobSearchMessage.objects.create(
            conversation_id=conv_id,
            role="user",
            content="in flight",
            idempotency_key=key,
        )
        JobSearchTurn.objects.create(
            conversation_id=conv_id,
            turn_key=key,
            delivery_state=JobSearchTurn.DeliveryState.PENDING,
            attempt_count=1,
            lease_expires_at=timezone.now() + timedelta(seconds=300),
        )
        history = self.client.get(
            reverse("agent-conversation-detail", args=[conv_id])
        ).json()
        user_entry = [m for m in history["messages"] if m["role"] == "user"][0]
        self.assertEqual(user_entry["delivery_state"], "pending")

        run_calls = {"n": 0}

        def counting_run_turn(*args, **kwargs):
            run_calls["n"] += 1
            return JobSearchService().run_turn(*args, **kwargs)

        with patch.object(
            JobSearchService, "run_turn", autospec=True,
            side_effect=counting_run_turn,
        ):
            resp = self._submit(conv_id, "in flight", key)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(run_calls["n"], 0)

    @override_settings(JOB_SEARCH_TURN_MAX_ATTEMPTS=2)
    def test_stale_pending_at_attempt_cap_is_finalized_not_retriable(self):
        """A stale claim already at the attempt cap is finalized as
        interrupted and rejected, instead of looping provider calls."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        self._stale_claim(conv_id, key, attempt_count=2)
        run_calls = {"n": 0}

        def counting_run_turn(*args, **kwargs):
            run_calls["n"] += 1
            return JobSearchService().run_turn(*args, **kwargs)

        with patch.object(
            JobSearchService, "run_turn", autospec=True,
            side_effect=counting_run_turn,
        ):
            resp = self._submit(conv_id, "interrupted", key)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"]["type"], "retry_limit_reached")
        self.assertEqual(run_calls["n"], 0)
        turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.FAILED)
        self.assertEqual(
            turn.failure_code, JobSearchTurn.FailureCode.WORKER_INTERRUPTED
        )

    # -- late-reply guard (adversarial review: reset/delete mid-turn) -------

    def test_reset_mid_turn_discards_late_reply(self):
        """A reply completing after the conversation was already reset is
        discarded: retryable 409, no assistant message persisted, turn
        quarantined."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())

        def reset_then_reply(*args, **kwargs):
            # The reset lands while the provider is still running.
            JobSearchConversation.objects.filter(pk=conv_id).update(active=False)
            return ("late reply", False, None)

        with patch.object(
            JobSearchService, "run_turn", autospec=True,
            side_effect=reset_then_reply,
        ):
            resp = self._submit(conv_id, "reply to nowhere", key)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        conv = JobSearchConversation.objects.get(pk=conv_id)
        self.assertFalse(conv.active)
        # The late reply was NOT attached to the (now archived) conversation.
        self.assertEqual(conv.messages.filter(role="assistant").count(), 0)
        turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.FAILED)
        self.assertEqual(
            turn.failure_code, JobSearchTurn.FailureCode.CONVERSATION_GONE
        )

    def test_reset_at_exact_attach_moment_discards_late_reply_409(self):
        """A reset landing at the EXACT attach moment — after the attach
        transaction's write-first claim matched, before the reply persisted,
        the TOCTOU window the round-2 review hooked — still discards the
        reply: no assistant row in the archived conversation, anchor not
        completed, retryable 409."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        real_reattach = job_search_views._reattach_locked_conversation

        def reset_between_read_and_persist(conversation_id, user):
            # The reset lands between the attach claim and the persist.
            JobSearchConversation.objects.filter(pk=conversation_id).update(
                active=False
            )
            return real_reattach(conversation_id, user)

        with patch.object(
            JobSearchService, "run_turn", autospec=True,
            side_effect=lambda *args, **kwargs: ("late reply", False, None),
        ), patch(
            "crank.views.job_search._reattach_locked_conversation",
            side_effect=reset_between_read_and_persist,
        ):
            resp = self._submit(conv_id, "reply to nowhere", key)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        conv = JobSearchConversation.objects.get(pk=conv_id)
        self.assertFalse(conv.active)
        # The late reply was NOT attached to the (now archived) conversation.
        self.assertEqual(conv.messages.filter(role="assistant").count(), 0)
        # The anchor was quarantined, never completed.
        turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.FAILED)
        self.assertEqual(
            turn.failure_code, JobSearchTurn.FailureCode.CONVERSATION_GONE
        )

    def test_delete_mid_turn_discards_late_reply(self):
        """A reply completing after the conversation was already deleted is
        discarded without raising: retryable 409, nothing persisted, cascades
        cleaned up."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())

        def delete_then_reply(*args, **kwargs):
            JobSearchConversation.objects.filter(pk=conv_id).delete()
            return ("late reply", False, None)

        with patch.object(
            JobSearchService, "run_turn", autospec=True,
            side_effect=delete_then_reply,
        ):
            resp = self._submit(conv_id, "reply to nowhere", key)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        self.assertFalse(JobSearchConversation.objects.filter(pk=conv_id).exists())
        # The late reply was not persisted anywhere.
        self.assertEqual(
            JobSearchMessage.objects.filter(
                role="assistant", content="late reply"
            ).count(),
            0,
        )
        # The anchor was removed by the cascade; finalizing it is a no-op.
        self.assertEqual(JobSearchTurn.objects.filter(turn_key=key).count(), 0)

    def test_delete_at_exact_attach_moment_discards_late_reply_409(self):
        """A delete landing at the EXACT attach moment — after the attach
        transaction's write-first claim matched, before the reply persisted
        — still discards the reply: nothing persisted, cascade removed the
        anchor, retryable 409."""
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        real_reattach = job_search_views._reattach_locked_conversation

        def delete_between_read_and_persist(conversation_id, user):
            # The delete lands between the attach claim and the persist.
            JobSearchConversation.objects.filter(pk=conversation_id).delete()
            return real_reattach(conversation_id, user)

        with patch.object(
            JobSearchService, "run_turn", autospec=True,
            side_effect=lambda *args, **kwargs: ("late reply", False, None),
        ), patch(
            "crank.views.job_search._reattach_locked_conversation",
            side_effect=delete_between_read_and_persist,
        ):
            resp = self._submit(conv_id, "reply to nowhere", key)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], "conversation_closed")
        self.assertFalse(JobSearchConversation.objects.filter(pk=conv_id).exists())
        self.assertEqual(
            JobSearchMessage.objects.filter(
                role="assistant", content="late reply"
            ).count(),
            0,
        )
        # The cascade removed the anchor; finalizing it was a safe no-op.
        self.assertEqual(JobSearchTurn.objects.filter(turn_key=key).count(), 0)

    # -- stable ordering (adversarial review: retry reordering) -------------

    def test_retry_reply_keeps_original_transcript_order(self):
        """A successful retry's reply is serialized immediately after the
        original turn, not appended after newer turns."""
        conv_id = self._start_conversation()
        first_key = str(uuid.uuid4())
        second_key = str(uuid.uuid4())
        real_run_turn = JobSearchService().run_turn.__func__

        # First turn fails; a newer turn completes while it is failed.
        with patch.object(
            JobSearchService, "run_turn", side_effect=ServiceTimeout("slow")
        ):
            self._submit(conv_id, "first question", first_key)
        self._submit(conv_id, "second question", second_key)

        # The retry's reply row is created LAST but must not render last.
        with patch.object(
            JobSearchService, "run_turn", autospec=True, side_effect=real_run_turn
        ):
            retry = self._submit(conv_id, "first question", first_key)
        self.assertEqual(retry.status_code, 201)

        history = self.client.get(
            reverse("agent-conversation-detail", args=[conv_id])
        ).json()
        roles = [m["role"] for m in history["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        user_contents = [
            m["content"] for m in history["messages"] if m["role"] == "user"
        ]
        self.assertEqual(user_contents, ["first question", "second question"])
        # The retried reply is pinned directly after the original question.
        self.assertEqual(history["messages"][0]["content"], "first question")
        self.assertEqual(history["messages"][1]["role"], "assistant")


class JobSearchTurnAnchorModelTests(TestCase):
    """The turn anchor's unconditional unique constraint (issue #458).

    The constraint must exist on every backend — MySQL included, where the
    message-level partial unique constraint is never emitted — because the
    anchor row is what makes apply-once hold in production. It is exercised
    directly here rather than via a conditional constraint.
    """

    def test_duplicate_turn_anchor_rejected(self):
        user = User.objects.create_user("anchormodel", "am@example.com", "pw")
        conversation = JobSearchConversation.objects.create(owner=user)
        JobSearchTurn.objects.create(
            conversation=conversation, turn_key="turn-key-a"
        )
        with self.assertRaises(IntegrityError):
            JobSearchTurn.objects.create(
                conversation=conversation, turn_key="turn-key-a"
            )

    def test_same_key_allowed_across_conversations(self):
        user = User.objects.create_user("anchormodel2", "am2@example.com", "pw")
        first = JobSearchConversation.objects.create(owner=user)
        second = JobSearchConversation.objects.create(owner=user)
        JobSearchTurn.objects.create(conversation=first, turn_key="shared")
        JobSearchTurn.objects.create(conversation=second, turn_key="shared")
        self.assertEqual(JobSearchTurn.objects.filter(turn_key="shared").count(), 2)

    def test_str_does_not_expose_turn_key(self):
        user = User.objects.create_user("anchormodel3", "am3@example.com", "pw")
        conversation = JobSearchConversation.objects.create(owner=user)
        turn = JobSearchTurn.objects.create(
            conversation=conversation, turn_key="secret-key"
        )
        rendered = str(turn)
        self.assertIn("pending", rendered)
        self.assertNotIn("secret-key", rendered)

    def test_delete_conversation_cascades_to_turns(self):
        user = User.objects.create_user("anchormodel4", "am4@example.com", "pw")
        conversation = JobSearchConversation.objects.create(owner=user)
        JobSearchTurn.objects.create(conversation=conversation, turn_key="k")
        conversation.delete()
        self.assertEqual(JobSearchTurn.objects.count(), 0)


class TurnClaimConcurrencyTests(TransactionTestCase):
    """Two-connection race for the turn claim (issue #458, adversarial review).

    Two threads with their own clients — and therefore their own database
    connections — submit the same key simultaneously, synchronized by a
    barrier at the POST. The winner's provider call is held open until the
    loser has responded, so exactly one request can win the anchor claim:
    exactly one provider execution, one user row, one assistant row, one
    anchor; the loser observes the live ``pending`` claim and gets 409.
    """

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_concurrent_same_key_submissions_run_provider_once(self):
        alice = User.objects.create_user("claimrace", "cr@example.com", "pw")
        client = Client()
        client.force_login(alice)

        create = client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 201)
        conversation_id = create.json()["id"]
        key = str(uuid.uuid4())

        run_calls = {"n": 0}
        real_run_turn = JobSearchService().run_turn.__func__
        release = threading.Event()
        barrier = threading.Barrier(2)

        def slow_run_turn(*args, **kwargs):
            run_calls["n"] += 1
            # Hold the winner's provider call open until the loser has
            # observed the pending claim and responded.
            release.wait(timeout=10)
            return real_run_turn(*args, **kwargs)

        def submit_once():
            # Two connections: each thread authenticates its own client, so
            # the two submissions run on separate database connections.
            local_client = Client()
            local_client.force_login(alice)
            barrier.wait(timeout=10)
            resp = local_client.post(
                reverse("agent-conversation-detail", args=[conversation_id]),
                data=json.dumps({"content": "once", "idempotency_key": key}),
                content_type="application/json",
            )
            # The loser releases the winner's provider call after its 409.
            release.set()
            return resp

        with patch.object(
            JobSearchService, "run_turn", autospec=True, side_effect=slow_run_turn
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(submit_once) for _ in range(2)]
                responses = [f.result() for f in futures]

        self.assertEqual(
            sorted(r.status_code for r in responses), [201, 409]
        )
        self.assertEqual(run_calls["n"], 1)
        conv = JobSearchConversation.objects.get(pk=conversation_id)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 1)
        # Exactly one anchor row exists and it completed.
        self.assertEqual(
            JobSearchTurn.objects.filter(conversation_id=conversation_id).count(), 1
        )
        turn = JobSearchTurn.objects.get(
            conversation_id=conversation_id, turn_key=key
        )
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.COMPLETED)
        self.assertEqual(turn.attempt_count, 1)


def _mysql_available():
    from django.db import connection

    return connection.vendor == "mysql"


@unittest.skipUnless(
    _mysql_available(),
    "requires a MySQL connection (production backend). Run it with the "
    "crank.settings.mysql_test module (same pattern as the score race tests): "
    "SECRET_KEY=test REDIS_MASTER_URL=redis://localhost:6379/0 "
    "DB_NAME=crank_test DB_USER=... DB_PASS=... DB_HOST=127.0.0.1 "
    "python -m pytest crank/tests/views/test_job_search.py -k mysql "
    "--ds crank.settings.mysql_test --create-db. CI has no MySQL service, so "
    "this proof runs there via the documented two-process staging drill.",
)
class TurnClaimMySQLConcurrencyTests(TransactionTestCase):
    """MySQL-backed apply-once proof for the turn anchor (issue #458).

    Real cross-connection contention against MySQL's default REPEATABLE READ
    isolation: the anchor's *unconditional* unique constraint plus the row
    lock serialize the submissions even though MySQL never emitted the
    message-level partial unique constraint (W036).
    """

    def test_concurrent_same_key_submissions_apply_outcome_once(self):
        alice = User.objects.create_user("mysqlclaim", "mc@example.com", "pw")
        client = Client()
        client.force_login(alice)
        cache.clear()
        create = client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 201)
        conversation_id = create.json()["id"]
        key = str(uuid.uuid4())

        run_calls = {"n": 0}
        real_run_turn = JobSearchService().run_turn.__func__

        def counting_run_turn(*args, **kwargs):
            run_calls["n"] += 1
            return real_run_turn(*args, **kwargs)

        with patch.object(
            JobSearchService, "run_turn", autospec=True, side_effect=counting_run_turn
        ):
            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(
                    pool.map(
                        lambda _: client.post(
                            reverse(
                                "agent-conversation-detail", args=[conversation_id]
                            ),
                            data=json.dumps(
                                {"content": "mysql once", "idempotency_key": key}
                            ),
                            content_type="application/json",
                        ),
                        range(4),
                    )
                )

        # Exactly one provider run and one applied outcome; every other
        # request is a replay or a stable 409.
        self.assertEqual(run_calls["n"], 1)
        self.assertEqual(
            sorted(r.status_code for r in responses),
            [200, 201, 409, 409],
        )
        conv = JobSearchConversation.objects.get(pk=conversation_id)
        self.assertEqual(conv.messages.filter(role="user").count(), 1)
        self.assertEqual(conv.messages.filter(role="assistant").count(), 1)
        self.assertEqual(
            JobSearchTurn.objects.filter(conversation_id=conversation_id).count(), 1
        )
        turn = JobSearchTurn.objects.get(
            conversation_id=conversation_id, turn_key=key
        )
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.COMPLETED)
        self.assertEqual(turn.attempt_count, 1)


@override_settings(CACHES=LOCMEM)
class RetryableConflictTurnStateTests(TestCase):
    """Retryable 409s release the turn anchor (issues #458 + #487).

    ``preference_stale`` and ``conversation_closed`` keep the user turn
    retryable with the same idempotency key, so the anchor must end
    ``failed`` — a ``pending`` claim would answer every retry with
    ``turn_in_progress`` until its lease expired.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("conflict", "c@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()

    def _start_conversation(self):
        resp = self.client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def _submit(self, conversation_id, key):
        return self.client.post(
            reverse("agent-conversation-detail", args=[conversation_id]),
            data=json.dumps({"content": "hello", "idempotency_key": key}),
            content_type="application/json",
        )

    def _assert_failed_then_retryable(self, error, error_type, failure_code):
        conv_id = self._start_conversation()
        key = str(uuid.uuid4())
        with patch.object(JobSearchService, "run_turn", side_effect=error):
            resp = self._submit(conv_id, key)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["type"], error_type)
        turn = JobSearchTurn.objects.get(conversation_id=conv_id, turn_key=key)
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.FAILED)
        self.assertEqual(turn.failure_code, failure_code)
        self.assertIsNone(turn.lease_expires_at)

        # The same key re-claims the turn instead of 409 turn_in_progress.
        with patch.object(
            JobSearchService, "run_turn", return_value=("retried", False, None)
        ):
            retry = self._submit(conv_id, key)
        self.assertEqual(retry.status_code, 201)
        turn.refresh_from_db()
        self.assertEqual(turn.delivery_state, JobSearchTurn.DeliveryState.COMPLETED)
        self.assertEqual(turn.attempt_count, 2)

    def test_preference_stale_releases_turn_for_same_key_retry(self):
        from crank.agents.job_search.demo import ServicePreferenceStale

        self._assert_failed_then_retryable(
            ServicePreferenceStale("stale"), "preference_stale", ""
        )

    def test_preference_version_unavailable_releases_turn(self):
        from crank.agents.job_search.demo import ServicePreferenceVersionUnavailable

        self._assert_failed_then_retryable(
            ServicePreferenceVersionUnavailable("no baseline"), "preference_stale", ""
        )

    def test_conversation_closed_releases_turn(self):
        from crank.agents.job_search.demo import ServiceConversationClosed

        self._assert_failed_then_retryable(
            ServiceConversationClosed("closed"),
            "conversation_closed",
            JobSearchTurn.FailureCode.CONVERSATION_GONE,
        )

    def test_release_tolerates_lock_contention(self):
        from django.db import OperationalError

        with patch.object(
            job_search_views, "_finalize_turn",
            side_effect=OperationalError("database is locked"),
        ):
            # Swallowed: lease expiry recovers the turn instead of a 500.
            job_search_views._release_failed_turn(1)

    def test_release_propagates_other_backend_errors(self):
        from django.db import OperationalError

        with patch.object(
            job_search_views, "_finalize_turn",
            side_effect=OperationalError("connection refused"),
        ):
            with self.assertRaises(OperationalError):
                job_search_views._release_failed_turn(1)
