# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from crank.models.agent_run import AgentRun
from crank.services import agent_runs


class ClaimRunMySQLAdvisoryLockTests(TestCase):
    """Tests for the MySQL advisory lock path in claim_run (MINOR-1)."""

    @patch("crank.services.agent_runs.acquire_advisory_lock", return_value=False)
    @patch("crank.services.agent_runs.monitoring.record_event")
    def test_claim_run_advisory_lock_failure_raises_integrity_error(self, mock_event, _lock):
        """When the MySQL advisory lock cannot be acquired, claim_run raises
        IntegrityError and emits a monitoring event."""
        with self.assertRaises(IntegrityError):
            agent_runs.claim_run(AgentRun.RunType.NOOP)
        mock_event.assert_called_once_with(
            "scheduled_run",
            {"run_type": AgentRun.RunType.NOOP, "status": "skipped",
             "reason_code": "overlap_advisory_lock"},
        )

    @patch("crank.services.agent_runs.acquire_advisory_lock", return_value=True)
    @patch("crank.services.agent_runs.release_advisory_lock")
    def test_claim_run_releases_advisory_lock_on_success(self, mock_release, _lock):
        """Advisory lock is released after a successful claim."""
        run = agent_runs.claim_run(AgentRun.RunType.NOOP)
        self.assertEqual(run.status, AgentRun.Status.RUNNING)
        mock_release.assert_called_once_with(AgentRun.RunType.NOOP)

    @patch("crank.services.agent_runs.acquire_advisory_lock", return_value=True)
    @patch("crank.services.agent_runs.release_advisory_lock")
    def test_claim_run_releases_advisory_lock_on_integrity_error(self, mock_release, _lock):
        """Advisory lock is released even when claim raises IntegrityError."""
        AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP,
            status=AgentRun.Status.RUNNING,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                agent_runs.claim_run(AgentRun.RunType.NOOP)
        mock_release.assert_called_once_with(AgentRun.RunType.NOOP)


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

    def test_claim_run_adopts_queued_pending_run(self):
        # A queued (admin-created) PENDING run is consumed by the next claim:
        # the same row transitions PENDING -> RUNNING and keeps its
        # correlation id instead of blocking the consumer (issue #462).
        pending = AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.PENDING,
        )
        run = agent_runs.claim_run(AgentRun.RunType.JOB_PIPELINE)
        self.assertEqual(run.pk, pending.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.RUNNING)
        self.assertIsNotNone(run.started_at)
        self.assertIsNone(run.finished_at)
        self.assertEqual(run.correlation_id, pending.correlation_id)
        # No second row was created; the queue was consumed, not bypassed.
        self.assertEqual(
            AgentRun.objects.filter(run_type=AgentRun.RunType.JOB_PIPELINE).count(),
            1,
        )

    def test_claim_run_adopts_pending_for_every_run_type(self):
        # Adoption is a general AgentRunCommand fix, not pipeline-specific:
        # queued rows for any run type become consumable work.
        for run_type in (
            AgentRun.RunType.CRAWL_SCHEDULE,
            AgentRun.RunType.GATHER_SCORES,
            AgentRun.RunType.NOOP,
        ):
            pending = AgentRun.objects.create(
                run_type=run_type, status=AgentRun.Status.PENDING
            )
            run = agent_runs.claim_run(run_type)
            self.assertEqual(run.pk, pending.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, AgentRun.Status.RUNNING)

    @override_settings(AGENT_RUN_STALE_AFTER_SECONDS=60)
    def test_stale_pending_run_is_reclaimed_as_failed(self):
        # A queued run no consumer ever adopted must not block the slot
        # forever: past the TTL it is finalized failed with an actionable
        # sanitized summary, and a fresh claim succeeds.
        stale = AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.PENDING,
        )
        AgentRun.objects.filter(pk=stale.pk).update(
            created=timezone.now() - timedelta(seconds=120)
        )
        run = agent_runs.claim_run(AgentRun.RunType.JOB_PIPELINE)
        stale.refresh_from_db()
        self.assertEqual(stale.status, AgentRun.Status.FAILED)
        self.assertIn("queued but never consumed", stale.error_summary)
        self.assertEqual(run.status, AgentRun.Status.RUNNING)
        self.assertNotEqual(run.pk, stale.pk)

    @patch("crank.services.agent_runs.newrelic.agent.record_custom_event")
    def test_adopted_pending_run_emits_run_started_event(self, mock_record):
        pending = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.PENDING
        )
        agent_runs.claim_run(AgentRun.RunType.NOOP)
        payload = mock_record.call_args.args[1]
        self.assertEqual(payload["eventType"], "run_started")
        self.assertEqual(payload["run_id"], pending.pk)
        self.assertEqual(payload["status"], "running")

    def test_claim_run_raises_integrity_error_when_running_exists(self):
        # Only a fresh RUNNING row blocks a new claim now: a queued PENDING row
        # is adopted (see adoption tests), a RUNNING row within the TTL is the
        # genuine overlap case.
        agent_runs.claim_run(AgentRun.RunType.NOOP)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                agent_runs.claim_run(AgentRun.RunType.NOOP)

    def test_record_skipped_marks_terminal_skipped(self):
        run = agent_runs.record_skipped(AgentRun.RunType.NOOP)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.SKIPPED)
        self.assertIsNotNone(run.finished_at)

    @patch("crank.services.agent_runs.monitoring.record_event")
    @patch("crank.services.agent_runs.newrelic.agent.record_custom_event")
    def test_record_skipped_emits_monitoring_event(self, _nr, mock_event):
        """NIT-3: record_skipped emits a low-cardinality overlap-skipped event."""
        agent_runs.record_skipped(AgentRun.RunType.NOOP, reason="overlap")
        # The overlap-skipped event is emitted with reason_code dimension
        mock_event.assert_any_call(
            "scheduled_run",
            {"run_type": AgentRun.RunType.NOOP, "status": "skipped", "reason_code": "overlap"},
        )

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


class ClaimRunAdoptionConcurrencyTests(TransactionTestCase):
    """A queued PENDING run is adopted at most once across contenders.

    Uses ``TransactionTestCase`` because threads do not share ``TestCase``'s
    transaction. The two claim attempts are serialized around a shared write
    lock (see the SQLite caveat documented in
    ``crank/tests/views/test_job_retrieval_admin.py``): the second contender
    observes the first's committed RUNNING row and is rejected by the overlap
    guard, so adoption happens exactly once and the queued row's identity is
    preserved. On MySQL the advisory lock plus partial-constraint semantics
    provide the same guarantee genuinely concurrently.
    """

    def test_pending_run_is_adopted_exactly_once(self):
        import threading

        pending = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP,
            status=AgentRun.Status.PENDING,
        )
        barrier = threading.Barrier(2)
        write_lock = threading.Lock()
        outcomes = {}

        def worker(index):
            barrier.wait()
            with write_lock:
                try:
                    with transaction.atomic():
                        run = agent_runs.claim_run(AgentRun.RunType.NOOP)
                    outcomes[index] = ("claimed", run.pk)
                except IntegrityError:
                    outcomes[index] = ("skipped", None)

        threads = [
            threading.Thread(target=worker, args=(index,)) for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        claimed = [pk for status, pk in outcomes.values() if status == "claimed"]
        self.assertEqual(len(claimed), 1, outcomes)
        self.assertEqual(claimed[0], pending.pk)
        pending.refresh_from_db()
        self.assertEqual(pending.status, AgentRun.Status.RUNNING)
        self.assertEqual(
            AgentRun.objects.filter(
                run_type=AgentRun.RunType.NOOP,
                status__in=[AgentRun.Status.RUNNING, AgentRun.Status.PENDING],
            ).count(),
            1,
        )
