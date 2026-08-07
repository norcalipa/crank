# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from crank.models.agent_run import AgentRun
from crank.services import agent_runs
from crank.management.base import AgentRunCommand


class AgentSettingTests(TestCase):
    def test_agent_runs_disabled_by_default(self):
        # Phase 1 foundation is off by default (roadmap docs/readme.md 8.1)
        # until secrets and policies are ready.
        self.assertFalse(settings.AGENT_RUN_ENABLED)
        self.assertFalse(settings.AGENT_NOOP_ENABLED)


class AgentNoopCommandTests(TestCase):
    def _call(self, stdout=None, stderr=None):
        stdout = stdout or StringIO()
        stderr = stderr or StringIO()
        code = call_command("agent_noop", stdout=stdout, stderr=stderr)
        return code, stdout, stderr

    @override_settings(AGENT_NOOP_ENABLED=False)
    def test_disabled_exits_zero_without_work(self):
        code, stdout, _ = self._call()
        self.assertEqual(code, 0)
        self.assertEqual(AgentRun.objects.count(), 0)
        self.assertIn("disabled", stdout.getvalue())

    @override_settings(AGENT_RUN_ENABLED=True, AGENT_NOOP_ENABLED=True)
    def test_enabled_records_succeeded_run(self):
        code, stdout, _ = self._call()
        self.assertEqual(code, 0)
        run = AgentRun.objects.get()
        self.assertEqual(run.run_type, AgentRun.RunType.NOOP)
        self.assertEqual(run.status, AgentRun.Status.SUCCEEDED)
        self.assertEqual(run.counts, {"items_seen": 0, "items_created": 0, "items_updated": 0, "items_failed": 0})
        self.assertIn("succeeded", stdout.getvalue())

    @override_settings(AGENT_RUN_ENABLED=True, AGENT_NOOP_ENABLED=True)
    def test_overlap_records_one_run_and_one_skipped(self):
        # Simulate an invocation that already claimed the slot.
        agent_runs.claim_run(AgentRun.RunType.NOOP)
        code, stdout, _ = self._call()
        self.assertEqual(code, 0)
        self.assertEqual(AgentRun.objects.count(), 2)
        statuses = set(AgentRun.objects.values_list("status", flat=True))
        self.assertEqual(statuses, {AgentRun.Status.RUNNING, AgentRun.Status.SKIPPED})
        self.assertIn("skipped", stdout.getvalue())

    @override_settings(AGENT_RUN_ENABLED=True, AGENT_NOOP_ENABLED=True)
    def test_exit_code(self):
        class FailingCommand(AgentRunCommand):
            run_type = "noop"
            enabled_setting = "AGENT_NOOP_ENABLED"

            def run_payload(self, run, **options):
                raise RuntimeError("boom")

        cmd = FailingCommand(stdout=StringIO(), stderr=StringIO())
        with self.assertRaises(CommandError):
            cmd.handle()
        run = AgentRun.objects.get(status=AgentRun.Status.FAILED)
        self.assertIn("boom", run.error_summary)