# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.db import IntegrityError, transaction
from django.test import TestCase

from crank.models.agent_run import AgentRun


class AgentRunModelTests(TestCase):
    def test_agent_run_creation_defaults(self):
        run = AgentRun.objects.create(run_type=AgentRun.RunType.NOOP)
        self.assertIsNotNone(run.correlation_id)
        self.assertEqual(run.status, AgentRun.Status.PENDING)
        self.assertEqual(run.counts, {})
        self.assertEqual(run.error_summary, "")
        self.assertIsNone(run.started_at)
        self.assertIsNone(run.finished_at)
        self.assertEqual(str(run), "noop [pending]")

    def test_agent_run_finalize_sets_counts_and_timestamps(self):
        run = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.RUNNING
        )
        run.finalize(
            AgentRun.Status.SUCCEEDED,
            counts={"items_seen": 3, "items_failed": 0},
        )
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.SUCCEEDED)
        self.assertEqual(run.counts, {"items_seen": 3, "items_failed": 0})
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.error_summary, "")

    def test_agent_run_finalize_failure_summary(self):
        run = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.RUNNING
        )
        run.finalize(AgentRun.Status.FAILED, error_summary="boom")
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.FAILED)
        self.assertEqual(run.error_summary, "boom")

    def test_only_one_running_run_per_type(self):
        AgentRun.objects.create(run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.RUNNING)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AgentRun.objects.create(
                    run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.RUNNING
                )

    def test_terminal_statuses_do_not_conflict_with_running_lock(self):
        # A terminal (succeeded) run must not block a new running run.
        AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.SUCCEEDED
        )
        second = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.RUNNING
        )
        self.assertEqual(second.status, AgentRun.Status.RUNNING)

    def test_finalize_rejects_invalid_transition(self):
        run = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.SUCCEEDED
        )
        with self.assertRaises(ValueError):
            run.finalize(AgentRun.Status.RUNNING)