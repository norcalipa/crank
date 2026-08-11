# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Management-command lifecycle tests for score gathering."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from crank.models import AgentRun
from crank.services import agent_runs


class GatherScoresCommandTests(TestCase):
    def call(self):
        stdout = StringIO()
        stderr = StringIO()
        code = call_command("gather_scores", stdout=stdout, stderr=stderr)
        return code, stdout, stderr

    @override_settings(GATHER_SCORES_ENABLED=False)
    def test_disabled_schedule_does_no_work(self):
        code, stdout, _ = self.call()
        self.assertEqual(code, 0)
        self.assertFalse(AgentRun.objects.exists())
        self.assertIn("disabled", stdout.getvalue())

    @override_settings(AGENT_RUN_ENABLED=True, GATHER_SCORES_ENABLED=True)
    def test_enabled_no_sources_succeeds_with_zero_counts(self):
        code, stdout, _ = self.call()
        self.assertEqual(code, 0)
        run = AgentRun.objects.get(run_type=AgentRun.RunType.GATHER_SCORES)
        self.assertEqual(run.status, AgentRun.Status.SUCCEEDED)
        self.assertEqual(run.counts["sources_total"], 0)
        self.assertIn("succeeded", stdout.getvalue())

    @override_settings(AGENT_RUN_ENABLED=True, GATHER_SCORES_ENABLED=True)
    def test_overlap_records_skipped_invocation(self):
        active = agent_runs.claim_run(AgentRun.RunType.GATHER_SCORES)
        with patch("crank.management.commands.gather_scores.gather_scores") as gather:
            code, stdout, _ = self.call()
        self.assertEqual(code, 0)
        gather.assert_not_called()
        self.assertEqual(
            set(AgentRun.objects.values_list("status", flat=True)),
            {AgentRun.Status.RUNNING, AgentRun.Status.SKIPPED},
        )
        self.assertIn("skipped", stdout.getvalue())
        active.refresh_from_db()
        self.assertEqual(active.status, AgentRun.Status.RUNNING)

    @override_settings(AGENT_RUN_ENABLED=True, GATHER_SCORES_ENABLED=True)
    def test_all_sources_failed_is_nonzero_and_preserves_counts(self):
        counts = {"sources_total": 1, "sources_succeeded": 0, "sources_failed": 1}
        error = RuntimeError("all sources failed")
        error.counts = counts
        with patch(
            "crank.management.commands.gather_scores.gather_scores",
            side_effect=error,
        ):
            with self.assertRaises(CommandError):
                self.call()
        run = AgentRun.objects.get(run_type=AgentRun.RunType.GATHER_SCORES)
        self.assertEqual(run.status, AgentRun.Status.FAILED)
        self.assertEqual(run.counts, counts)
        self.assertIn("all sources failed", run.error_summary)

    @override_settings(AGENT_RUN_ENABLED=True, GATHER_SCORES_ENABLED=True)
    def test_service_counts_are_recorded_in_success_event(self):
        counts = {"sources_total": 0, "sources_succeeded": 0, "sources_failed": 0}
        with patch(
            "crank.management.commands.gather_scores.gather_scores",
            return_value=counts,
        ), patch("crank.services.agent_runs.record_agent_event") as event:
            code, _, _ = self.call()
        self.assertEqual(code, 0)
        run = AgentRun.objects.get()
        self.assertEqual(run.status, AgentRun.Status.SUCCEEDED)
        self.assertTrue(
            any(
                call.args[0].pk == run.pk
                and call.args[1] == "run_succeeded"
                and call.kwargs == {"counts": counts}
                for call in event.call_args_list
            )
        )
