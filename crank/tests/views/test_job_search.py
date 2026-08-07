# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
Tests for the Phase 1 authenticated job-search chat transport.

Covers auth, ownership, CSRF, malformed/oversized payloads, idempotent retry,
service errors, rate limiting, and no cross-user leakage.
"""
import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from crank.agents.job_search.demo import JobSearchService, JobSearchServiceError
from crank.models import JobSearchConversation, JobSearchMessage


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


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
        from crank.agents.job_search.service import _build_provider
        with self.assertRaises(JobSearchServiceError):
            _build_provider()

    def test_service_run_turn_success_and_truncation(self):
        from crank.agents.job_search.service import DemoJobSearchProvider
        conv = JobSearchConversation.objects.create(owner=self.user)
        svc = JobSearchService(DemoJobSearchProvider())
        reply, changed = svc.run_turn(conversation=conv, user_message="salary")
        self.assertTrue(reply)
        self.assertTrue(changed)
        JobSearchMessage.objects.create(
            conversation=conv, role="user", content="salary"
        )
        JobSearchMessage.objects.create(
            conversation=conv, role="user", content="culture"
        )
        reply2, _ = svc.run_turn(conversation=conv, user_message="more")
        self.assertIn("more", reply2)
        with override_settings(JOB_SEARCH_RESPONSE_MAX_LEN=10):
            reply3, _ = svc.run_turn(conversation=conv, user_message="again")
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
        from rest_framework.exceptions import ValidationError as DRFValidationError
        from crank.serializers import job_search as jser
        with self.assertRaises(DRFValidationError):
            jser.MessageSubmitSerializer().validate_content("")

    def test_rate_limit_allows_anonymous(self):
        from crank.views.job_search import _check_rate_limit
        from django.test import RequestFactory
        anon = Client()
        req = RequestFactory().post("/")
        req.user = type("Anonymous", (), {"is_authenticated": False})()
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
