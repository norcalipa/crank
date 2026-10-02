# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Assistant success telemetry (issue #482): exact, bounded event sequences."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crank.agents.job_search.demo import (
    AssistantUnavailable,
    JobSearchService,
    ServiceConversationClosed,
    ServiceCostLimit,
    ServiceInvalidOutput,
    ServiceTimeout,
)
from crank.agents.job_search.types import JobResult, StructuredResults
from crank.models import JobSearchConversation, JobSearchTurn
from crank.services import monitoring
from crank.services.preferences import apply_patch_to_user, token_owner

RECORD = "crank.views.job_search.monitoring.record_event"
SECRET_PROMPT = "remote jobs in sf for user@example.com https://evil.example/x"


def _results():
    return StructuredResults(
        jobs=(JobResult(id=1, title="Engineer", organization_name="Acme", location="SF", remote=True),)
    )


def _events(record, name="assistant_turn"):
    return [c.args[1] for c in record.call_args_list if c.args[0] == name]


def _phases(record):
    return [e["phase"] for e in _events(record)]


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("telemetry", "t@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()

    def start(self):
        resp = self.client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def submit(self, conv_id, key=None, content=SECRET_PROMPT, **extra):
        return self.client.post(
            reverse("agent-conversation-detail", args=[conv_id]),
            data=json.dumps({"content": content, "idempotency_key": key or str(uuid.uuid4())}),
            content_type="application/json",
            **extra,
        )

    def run_with(self, result):
        return patch.object(JobSearchService, "run_turn", autospec=True, side_effect=result)


def _reply(text="ok", results=None):
    def _run(self, conversation, user_message, persist_reply=None):
        return text, False, results
    return _run


class AssistantTurnEventTests(_Base):
    @patch(RECORD)
    def test_success_without_results(self, record):
        conv = self.start()
        with self.run_with(_reply()):
            self.assertEqual(self.submit(conv).status_code, 201)
        self.assertEqual(_phases(record), ["attempted", "saved", "replied"])
        attempted, _, replied = _events(record)
        self.assertEqual((attempted["attempt"], attempted["retry"]), (1, False))
        self.assertIs(replied["results"], False)
        self.assertIn("latency_bucket", replied)
        self.assertEqual(_events(record, "assistant_first_result"), [])
        self.assertIsNone(JobSearchConversation.objects.get(pk=conv).first_result_at)

    @patch(RECORD)
    def test_first_result_once_per_conversation(self, record):
        conv = self.start()
        with self.run_with(_reply(results=_results())):
            self.assertEqual(self.submit(conv).status_code, 201)
            self.assertEqual(self.submit(conv).status_code, 201)
        first = _events(record, "assistant_first_result")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["turns_to_first_result"], 1)
        self.assertGreaterEqual(first[0]["seconds_to_first_result"], 0)
        self.assertIsNotNone(JobSearchConversation.objects.get(pk=conv).first_result_at)
        self.assertEqual(_phases(record).count("replied"), 2)

    @patch(RECORD)
    def test_pre_existing_results_claim_marker_without_emitting(self, record):
        from crank.models import JobSearchMessage

        conv = self.start()
        for _ in range(3):
            JobSearchMessage.objects.create(
                conversation_id=conv, role=JobSearchMessage.Role.ASSISTANT,
                content="earlier", results_json='[{"id": 1}]', idempotency_key=str(uuid.uuid4()),
            )
        JobSearchConversation.objects.filter(pk=conv).update(
            created=timezone.now() - timedelta(days=20)
        )
        with self.run_with(_reply(results=_results())):
            self.assertEqual(self.submit(conv).status_code, 201)
            self.assertEqual(self.submit(conv).status_code, 201)
        self.assertEqual(_events(record, "assistant_first_result"), [])
        self.assertIsNotNone(JobSearchConversation.objects.get(pk=conv).first_result_at)

    @patch(RECORD)
    def test_first_result_on_second_turn_counts_turns(self, record):
        conv = self.start()
        with self.run_with(_reply()):
            self.submit(conv)
        with self.run_with(_reply(results=_results())):
            self.submit(conv)
        first = _events(record, "assistant_first_result")
        self.assertEqual([e["turns_to_first_result"] for e in first], [2])

    @patch(RECORD)
    def test_failure_paths_emit_one_failed_event_with_stage(self, record):
        cases = [
            (AssistantUnavailable("x"), "assistant_unavailable", "availability"),
            (ServiceTimeout("x"), "provider_timeout", "provider"),
            (ServiceCostLimit("x"), "cost_limit", "provider"),
            (ServiceInvalidOutput("x"), "invalid_output", "provider"),
            (ServiceConversationClosed("x"), "conversation_gone", "lifecycle"),
            (RuntimeError("secret detail"), "unexpected_error", "internal"),
        ]
        for exc, code, stage in cases:
            record.reset_mock()
            conv = self.start()
            with patch.object(JobSearchService, "run_turn", side_effect=exc):
                self.submit(conv)
            failed = [e for e in _events(record) if e["phase"] == "failed"]
            self.assertEqual(len(failed), 1, code)
            self.assertEqual((failed[0]["reason_code"], failed[0]["failure_stage"]), (code, stage))
            self.assertEqual(_events(record, "assistant_first_result"), [])

    @patch(RECORD)
    def test_service_error_failure(self, record):
        from crank.agents.job_search.demo import JobSearchServiceError

        conv = self.start()
        with patch.object(JobSearchService, "run_turn", side_effect=JobSearchServiceError("x")):
            self.submit(conv)
        failed = [e for e in _events(record) if e["phase"] == "failed"]
        self.assertEqual([(e["reason_code"], e["failure_stage"]) for e in failed], [("service_error", "internal")])

    @patch(RECORD)
    def test_retry_after_failure_is_attempt_two(self, record):
        conv = self.start()
        key = str(uuid.uuid4())
        with patch.object(JobSearchService, "run_turn", side_effect=ServiceTimeout("x")):
            self.assertEqual(self.submit(conv, key).status_code, 504)
        with self.run_with(_reply()):
            self.assertEqual(self.submit(conv, key).status_code, 201)
        attempted = [e for e in _events(record) if e["phase"] == "attempted"]
        self.assertEqual([(e["attempt"], e["retry"]) for e in attempted], [(1, False), (2, True)])
        saved = [e for e in _events(record) if e["phase"] == "saved"]
        self.assertEqual(len(saved), 1)

    @patch(RECORD)
    def test_replay_of_completed_turn(self, record):
        conv = self.start()
        key = str(uuid.uuid4())
        with self.run_with(_reply()):
            self.submit(conv, key)
        record.reset_mock()
        self.assertEqual(self.submit(conv, key).status_code, 200)
        self.assertEqual(_phases(record), ["replayed"])

    @patch(RECORD)
    def test_replay_via_completed_anchor(self, record):
        conv = self.start()
        with self.run_with(_reply()):
            self.submit(conv)
        stored = JobSearchConversation.objects.get(pk=conv).messages.last()
        record.reset_mock()
        with patch.object(
            job_search_view_module(), "_claim_turn", return_value=(None, "completed", stored)
        ):
            self.assertEqual(self.submit(conv).status_code, 200)
        self.assertEqual(_phases(record), ["replayed"])

    @override_settings(JOB_SEARCH_RATE_LIMIT_PER_HOUR=1)
    @patch(RECORD)
    def test_rate_limited_rejection(self, record):
        conv = self.start()
        with self.run_with(_reply()):
            self.submit(conv)
            record.reset_mock()
            self.assertEqual(self.submit(conv).status_code, 429)
        self.assertEqual(_phases(record), ["rejected"])
        event = _events(record)[0]
        self.assertEqual((event["reason_code"], event["failure_stage"]), ("rate_limited", "capacity"))

    @patch(RECORD)
    def test_turn_in_progress_rejection(self, record):
        conv = self.start()
        key = str(uuid.uuid4())
        JobSearchTurn.objects.create(
            conversation_id=conv, turn_key=key,
            delivery_state=JobSearchTurn.DeliveryState.PENDING, attempt_count=1,
            lease_expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.assertEqual(self.submit(conv, key).status_code, 409)
        self.assertEqual(_phases(record), ["rejected"])
        self.assertEqual(_events(record)[0]["reason_code"], "turn_in_progress")

    @override_settings(JOB_SEARCH_TURN_MAX_ATTEMPTS=1)
    @patch(RECORD)
    def test_retry_limited_rejection(self, record):
        conv = self.start()
        key = str(uuid.uuid4())
        with patch.object(JobSearchService, "run_turn", side_effect=ServiceTimeout("x")):
            self.submit(conv, key)
        record.reset_mock()
        self.assertEqual(self.submit(conv, key).status_code, 429)
        self.assertEqual(_phases(record), ["rejected"])
        self.assertEqual(_events(record)[0]["reason_code"], "retry_limited")

    @patch(RECORD)
    def test_reaped_worker_interrupted_event(self, record):
        conv = self.start()
        key = str(uuid.uuid4())
        JobSearchTurn.objects.create(
            conversation_id=conv, turn_key=key,
            delivery_state=JobSearchTurn.DeliveryState.PENDING, attempt_count=1,
            lease_expires_at=timezone.now() - timedelta(minutes=5),
        )
        detail = reverse("agent-conversation-detail", args=[conv])
        self.assertEqual(self.client.get(detail).status_code, 200)
        self.assertEqual(self.client.get(detail).status_code, 200)
        reaped = [e for e in _events(record) if e["phase"] == "failed"]
        self.assertEqual(len(reaped), 1)
        self.assertEqual(reaped[0]["reason_code"], "worker_interrupted")
        self.assertEqual(reaped[0]["turns"], 1)

    @patch(RECORD)
    def test_stale_pending_takeover_emits_worker_interrupted(self, record):
        conv = self.start()
        key = str(uuid.uuid4())
        JobSearchTurn.objects.create(
            conversation_id=conv, turn_key=key,
            delivery_state=JobSearchTurn.DeliveryState.PENDING, attempt_count=1,
            lease_expires_at=timezone.now() - timedelta(minutes=5),
        )
        with self.run_with(_reply()):
            self.assertEqual(self.submit(conv, key).status_code, 201)
        self.assertEqual(_phases(record), ["failed", "attempted", "saved", "replied"])
        failed, attempted = _events(record)[:2]
        self.assertEqual(failed["reason_code"], "worker_interrupted")
        self.assertEqual(failed["failure_stage"], "internal")
        self.assertEqual(failed["turns"], 1)
        self.assertEqual((attempted["attempt"], attempted["retry"]), (2, True))

    @patch(RECORD)
    def test_failed_turn_retry_is_not_counted_as_interrupted(self, record):
        conv = self.start()
        key = str(uuid.uuid4())
        with patch.object(JobSearchService, "run_turn", side_effect=ServiceTimeout("x")):
            self.submit(conv, key)
        record.reset_mock()
        with self.run_with(_reply()):
            self.assertEqual(self.submit(conv, key).status_code, 201)
        self.assertEqual(_phases(record), ["attempted", "replied"])

    @patch(RECORD)
    @override_settings(JOB_SEARCH_TURN_MAX_ATTEMPTS=2)
    def test_stale_pending_at_attempt_cap_emits_worker_interrupted(self, record):
        conv = self.start()
        key = str(uuid.uuid4())
        JobSearchTurn.objects.create(
            conversation_id=conv, turn_key=key,
            delivery_state=JobSearchTurn.DeliveryState.PENDING, attempt_count=2,
            lease_expires_at=timezone.now() - timedelta(minutes=5),
        )
        self.assertEqual(self.submit(conv, key).status_code, 429)
        self.assertEqual(_phases(record), ["failed", "rejected"])
        failed, rejected = _events(record)
        self.assertEqual((failed["reason_code"], failed["turns"]), ("worker_interrupted", 1))
        self.assertEqual(rejected["reason_code"], "retry_limited")
        turn = JobSearchTurn.objects.get(turn_key=key)
        self.assertEqual(turn.failure_code, JobSearchTurn.FailureCode.WORKER_INTERRUPTED)

    @patch(RECORD)
    @override_settings(JOB_SEARCH_TURN_MAX_ATTEMPTS=2)
    def test_failed_turn_at_attempt_cap_only_rejects(self, record):
        conv = self.start()
        key = str(uuid.uuid4())
        JobSearchTurn.objects.create(
            conversation_id=conv, turn_key=key,
            delivery_state=JobSearchTurn.DeliveryState.FAILED, attempt_count=2,
        )
        self.assertEqual(self.submit(conv, key).status_code, 429)
        self.assertEqual(_phases(record), ["rejected"])

    @patch(RECORD)
    def test_midturn_reset_emits_conversation_gone(self, record):
        class ResetProvider:
            def generate_reply(self, *, conversation, user_message):
                JobSearchConversation.objects.filter(pk=conversation.pk).update(active=False)
                JobSearchConversation.objects.create(owner=conversation.owner)
                return "late", True, None

        conv = self.start()
        with patch.object(
            JobSearchService, "__init__",
            lambda self, *a, **k: setattr(self, "provider", ResetProvider()),
        ):
            self.assertEqual(self.submit(conv).status_code, 409)
        failed = [e for e in _events(record) if e["phase"] == "failed"]
        self.assertEqual([e["reason_code"] for e in failed], ["conversation_gone"])
        self.assertEqual(_events(record, "assistant_first_result"), [])


def job_search_view_module():
    from crank.views import job_search

    return job_search


class HostileInputTests(_Base):
    def test_client_request_id_and_prompt_never_reach_new_relic(self):
        conv = self.start()
        with patch("crank.services.monitoring.newrelic.agent.record_custom_event") as nr:
            with self.run_with(_reply("a reply with user@example.com", results=_results())):
                self.submit(conv, HTTP_X_REQUEST_ID="https://evil.example/x?u=user:42")
            with patch.object(JobSearchService, "run_turn", side_effect=ServiceTimeout("remote jobs in sf")):
                self.submit(conv, HTTP_X_REQUEST_ID="user:42")
        self.assertTrue(nr.call_args_list)
        blob = json.dumps([c.args[1] for c in nr.call_args_list])
        for needle in ("evil.example", "user:42", "user@example.com", "remote jobs in sf"):
            self.assertNotIn(needle, blob)
        for call in nr.call_args_list:
            payload = call.args[1]
            self.assertNotIn("correlation_id", payload)
            for key, value in payload.items():
                self.assertIsInstance(value, (bool, int, float, str), key)

    def test_uuid_request_id_is_kept(self):
        conv = self.start()
        rid = str(uuid.uuid4())
        with patch("crank.services.monitoring.newrelic.agent.record_custom_event") as nr:
            with patch.object(JobSearchService, "run_turn", side_effect=ServiceTimeout("x")):
                self.submit(conv, HTTP_X_REQUEST_ID=rid)
        self.assertTrue(any(c.args[1].get("correlation_id") == rid for c in nr.call_args_list))


class FirstResultConcurrencyTest(TransactionTestCase):
    def test_concurrent_first_results_emit_exactly_once(self):
        user = User.objects.create_user("firstresult", "f@example.com", "pw")
        client = Client()
        client.force_login(user)
        cache.clear()
        conv = client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        ).json()["id"]

        def post(i):
            return client.post(
                reverse("agent-conversation-detail", args=[conv]),
                data=json.dumps({"content": "race %d" % i, "idempotency_key": str(uuid.uuid4())}),
                content_type="application/json",
            )

        with patch.object(JobSearchService, "run_turn", autospec=True, side_effect=_reply(results=_results())):
            with patch(RECORD) as record:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    list(pool.map(post, range(6)))
        self.assertEqual(len(_events(record, "assistant_first_result")), 1)


class PreferenceDecisionEventTests(_Base):
    def post(self, name, payload):
        return self.client.post(
            reverse(name), data=json.dumps(payload), content_type="application/json"
        )

    @patch(RECORD)
    def test_dismiss_apply_stale_invalid_and_undo(self, record):
        token = {
            "patch": {"set": {"notes": "n1"}}, "scope": "account",
            "base_revision": 0, "owner": token_owner(self.user),
        }
        self.assertEqual(self.post("agent-preference-apply", {"decision": "dismiss"}).status_code, 200)
        self.assertEqual(self.post("agent-preference-apply", {"proposal": token, "decision": "apply"}).status_code, 200)
        self.assertEqual(self.post("agent-preference-apply", {"proposal": token, "decision": "apply"}).status_code, 409)
        self.assertEqual(self.post("agent-preference-apply", {"proposal": dict(token, base_revision="x"), "decision": "apply"}).status_code, 400)
        search = dict(token, scope="search", base_revision=1)
        self.assertEqual(self.post("agent-preference-apply", {"proposal": search, "decision": "apply"}).status_code, 200)
        applied = apply_patch_to_user(self.user, {"set": {"notes": "n2"}})
        self.assertEqual(self.post("agent-preference-undo", {"undo": applied["undo"]}).status_code, 200)
        self.assertEqual(self.post("agent-preference-undo", {"undo": None}).status_code, 400)
        got = [(e["decision"], e["scope"], e["status"], e["origin"]) for e in _events(record, "preference_decision")]
        self.assertEqual(
            got,
            [
                ("dismiss", "account", "dismissed", "proposal"),
                ("apply", "account", "applied", "proposal"),
                ("apply", "account", "stale", "proposal"),
                ("apply", "account", "invalid", "proposal"),
                ("apply", "search", "applied", "proposal"),
                ("undo", "account", "undone", "proposal"),
                ("undo", "account", "invalid", "proposal"),
            ],
        )

    def test_failed_status_for_server_errors(self):
        from django.http import JsonResponse
        from crank.views import job_search as view

        with patch(RECORD) as record:
            view._preference_decision_event(
                JsonResponse({}, status=500), decision="apply", scope="account", ok_status="applied"
            )
        self.assertEqual(_events(record, "preference_decision")[0]["status"], "failed")
