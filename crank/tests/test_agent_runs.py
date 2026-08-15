# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from crank.models.agent_run import AgentRun
from crank.services import agent_runs


class AgentRunService(TestCase):
    def test_claim_run_creates_running_run(self):
        run = agent_runs.claim_run(AgentRun.RunType.NOOP)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.RUNNING)
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.correlation_id)

    def test_claim_run_overlap_raises_integrity_error(self):
        agent_runs.claim_run(AgentRun.RunType.NOOP)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                agent_runs.claim_run(AgentRun.RunType.NOOP)

    @override_settings(AGENT_RUN_STALE_AFTER_SECONDS=60)
    def test_stale_running_run_is_reclaimed(self):
        # A claim that was never finalized (crash) must not block the run type
        # forever: it is reclaimed as failed and a fresh claim is allowed.
        stale = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP,
            status=AgentRun.Status.RUNNING,
            started_at=timezone.now() - timedelta(hours=2),
        )
        run = agent_runs.claim_run(AgentRun.RunType.NOOP)
        stale.refresh_from_db()
        self.assertEqual(stale.status, AgentRun.Status.FAILED)
        self.assertIn("Stale", stale.error_summary)
        self.assertEqual(run.status, AgentRun.Status.RUNNING)
        self.assertNotEqual(run.pk, stale.pk)

    def test_claim_run_raises_integrity_error_when_pending_exists(self):
        # An admin-queued PENDING run must block the scheduler's claim path:
        # a RUNNING claim cannot be created alongside it (at-most-one-active).
        AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP,
            status=AgentRun.Status.PENDING,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                agent_runs.claim_run(AgentRun.RunType.NOOP)
        self.assertEqual(
            AgentRun.objects.filter(run_type=AgentRun.RunType.NOOP).count(),
            1,
        )

    def test_record_skipped_marks_terminal_skipped(self):
        run = agent_runs.record_skipped(AgentRun.RunType.NOOP)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.SKIPPED)
        self.assertIsNotNone(run.finished_at)

    def test_finalize_success_logs_counters(self):
        run = agent_runs.claim_run(AgentRun.RunType.NOOP)
        agent_runs.finalize_success(run, counts={"items_seen": 1})
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.SUCCEEDED)
        self.assertEqual(run.counts, {"items_seen": 1})

    def test_finalize_failure_persists_sanitized_summary(self):
        run = agent_runs.claim_run(AgentRun.RunType.NOOP)
        agent_runs.finalize_failure(run, ValueError("boom: token=abc123"))
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.FAILED)
        self.assertEqual(run.error_summary, "boom: <redacted>")

    @patch("crank.services.agent_runs.newrelic.agent.record_custom_event")
    def test_claim_run_emits_new_relic_event(self, mock_record):
        run = agent_runs.claim_run(AgentRun.RunType.NOOP)
        mock_record.assert_called_once()
        payload = mock_record.call_args.args[1]
        self.assertEqual(payload["eventType"], "run_started")
        self.assertEqual(payload["run_type"], "noop")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["run_id"], run.pk)

    @patch("crank.services.agent_runs.newrelic.agent.record_custom_event")
    def test_new_relic_failure_does_not_break_the_run(self, _mock_record):
        class ExplodingEvent:
            @staticmethod
            def record_custom_event(*args, **kwargs):
                raise RuntimeError("new relic down")

        with patch("crank.services.agent_runs.newrelic.agent", ExplodingEvent):
            run = agent_runs.claim_run(AgentRun.RunType.NOOP)
        self.assertEqual(run.status, AgentRun.Status.RUNNING)


class SanitizeErrorTests(TestCase):
    def test_sanitize_redacts_secrets_and_bounds_length(self):
        message = "HTTP 500 fetching https://api.example.com with key=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        out = agent_runs.sanitize_error(ValueError(message))
        self.assertNotIn("deadbeef", out)
        self.assertLessEqual(len(out), agent_runs.ERROR_SUMMARY_MAX_LENGTH)

    def test_sanitize_redacts_bearer_tokens(self):
        out = agent_runs.sanitize_error(ValueError("unauthorized Bearer abcDEF123_-./~"))
        self.assertNotIn("abcDEF123_-./~", out)

    def test_sanitize_collapses_whitespace(self):
        out = agent_runs.sanitize_error(ValueError("line one\n   line two"))
        self.assertEqual(out, "line one line two")

    def test_sanitize_none_returns_empty(self):
        self.assertEqual(agent_runs.sanitize_error(None), "")