# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the rollback drill management command."""

import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta

from crank.models.agent_run import AgentRun
from crank.models.monitoring import CapabilitySwitch, OperationalChangeAudit


class RollbackDrillCommandTests(TestCase):
    """Tests for the rollback_drill management command."""

    def _call(self, stdout=None, stderr=None, as_json=False):
        stdout = stdout or StringIO()
        stderr = stderr or StringIO()
        args = []
        if as_json:
            args.append("--json")
        code = call_command("rollback_drill", *args, stdout=stdout, stderr=stderr)
        return code, stdout, stderr

    def test_drill_passes_with_no_running_runs(self):
        """The drill passes when there are no orphaned RUNNING runs."""
        code, stdout, _ = self._call()
        self.assertEqual(code, 0)
        self.assertIn("passed", stdout.getvalue())

    def test_json_output_is_valid_json(self):
        """The --json flag produces valid JSON with expected keys."""
        code, stdout, _ = self._call(as_json=True)
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "passed")
        self.assertIn("capabilities", report)
        self.assertIn("data_consistency", report)
        self.assertIn("drilled_at", report)
        self.assertEqual(len(report["capabilities"]), 3)

    def test_json_capabilities_have_required_fields(self):
        """Each capability result has the expected fields."""
        code, stdout, _ = self._call(as_json=True)
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        for cap in report["capabilities"]:
            self.assertIn("key", cap)
            self.assertIn("run_type", cap)
            self.assertIn("switch_enabled", cap)
            self.assertIn("capability_blocked", cap)
            self.assertIn("run_type_blocked", cap)
            self.assertIn("passed", cap)

    def test_drill_creates_disabled_switches(self):
        """The drill creates CapabilitySwitch entries with enabled=False."""
        self._call()
        for key in ("interactive_agent", "gather_scores", "job_pipeline"):
            switch = CapabilitySwitch.objects.get(key=key)
            self.assertFalse(switch.enabled)
            self.assertIn("rollback drill", switch.note)

    def test_drill_disables_existing_enabled_switch(self):
        """If a switch already exists and is enabled, the drill disables it."""
        switch = CapabilitySwitch.objects.create(
            key="interactive_agent", enabled=True, note="original"
        )
        self._call()
        switch.refresh_from_db()
        self.assertFalse(switch.enabled)
        self.assertEqual(switch.note, "rollback drill")

    def test_drill_does_not_create_agent_runs(self):
        """The drill must not create any AgentRun rows."""
        count_before = AgentRun.objects.count()
        self._call()
        self.assertEqual(AgentRun.objects.count(), count_before)

    def test_drill_records_operational_change_audit(self):
        """The drill records an OperationalChangeAudit entry."""
        self.assertFalse(OperationalChangeAudit.objects.exists())
        self._call()
        audit = OperationalChangeAudit.objects.get(
            target_type="rollback_drill",
            target_id="staging",
            action="rollback_drill",
        )
        self.assertTrue(audit.confirmed)
        self.assertIn("capabilities_drilled", audit.new_value)
        self.assertTrue(audit.new_value["overall_passed"])

    def test_drill_emits_monitoring_event(self):
        """The drill emits a monitoring event for the rollback drill."""
        with patch("crank.management.commands.rollback_drill.monitoring.record_event") as event:
            self._call()
        event.assert_called_once_with(
            "operational_change",
            {
                "action": "rollback_drill",
                "capability": "all",
                "confirmed": True,
            },
        )

    def test_drill_fails_on_orphaned_running_run(self):
        """The drill fails when a stale RUNNING run exists beyond the TTL."""
        from django.core.management.base import CommandError

        stale_time = timezone.now() - timedelta(seconds=7200)
        AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP,
            status=AgentRun.Status.RUNNING,
            started_at=stale_time,
        )
        with self.assertRaises(CommandError):
            self._call()

    def test_drill_json_fails_on_orphaned_running_run(self):
        """JSON output reports failure when orphaned runs exist."""
        from django.core.management.base import CommandError

        stale_time = timezone.now() - timedelta(seconds=7200)
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.RUNNING,
            started_at=stale_time,
        )
        stdout = StringIO()
        stderr = StringIO()
        with self.assertRaises(CommandError):
            call_command("rollback_drill", "--json", stdout=stdout, stderr=stderr)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "failed")
        self.assertGreater(report["data_consistency"]["orphaned_running"], 0)

    def test_drill_passes_with_recent_running_run(self):
        """A recently started RUNNING run is not orphaned."""
        AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP,
            status=AgentRun.Status.RUNNING,
            started_at=timezone.now(),
        )
        code, stdout, _ = self._call()
        self.assertEqual(code, 0)
        self.assertIn("passed", stdout.getvalue())

    def test_human_report_lists_all_capabilities(self):
        """Human-readable output lists each capability."""
        code, stdout, _ = self._call()
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("interactive_agent", output)
        self.assertIn("gather_scores", output)
        self.assertIn("job_pipeline", output)
        self.assertIn("Data consistency:", output)

    def test_capability_blocked_is_true_for_all_drilled_switches(self):
        """All three capability switches are blocked after the drill."""
        code, stdout, _ = self._call(as_json=True)
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        for cap in report["capabilities"]:
            self.assertTrue(cap["capability_blocked"])
            self.assertTrue(cap["passed"])

    def test_run_type_blocked_for_matching_keys(self):
        """For gather_scores and job_pipeline, run_type_blocked is True."""
        code, stdout, _ = self._call(as_json=True)
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        for cap in report["capabilities"]:
            if cap["key"] in ("gather_scores", "job_pipeline"):
                self.assertTrue(
                    cap["run_type_blocked"],
                    f"run_type_blocked should be True for {cap['key']}",
                )

    def test_drill_fails_when_capability_not_blocked(self):
        """The drill reports failure when a capability switch fails to block."""
        from django.core.management.base import CommandError

        with patch(
            "crank.management.commands.rollback_drill.monitoring.capability_enabled",
            return_value=True,
        ):
            with self.assertRaises(CommandError):
                self._call()

    def test_drill_is_idempotent(self):
        """Running the drill twice does not duplicate switches or audits."""
        self._call()
        self._call()
        self.assertEqual(CapabilitySwitch.objects.count(), 3)
        self.assertEqual(
            OperationalChangeAudit.objects.filter(
                target_type="rollback_drill"
            ).count(),
            2,
        )
