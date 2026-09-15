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
from crank.models.monitoring import (
    ALLOWED_CAPABILITY_KEYS,
    CapabilitySwitch,
    OperationalChangeAudit,
)


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
        self.assertEqual(len(report["capabilities"]), len(ALLOWED_CAPABILITY_KEYS))

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
        self.assertEqual(CapabilitySwitch.objects.count(), len(ALLOWED_CAPABILITY_KEYS))
        self.assertEqual(
            OperationalChangeAudit.objects.filter(
                target_type="rollback_drill"
            ).count(),
            2,
        )

    def test_drill_exercises_every_registered_key(self):
        """Lockstep contract: the drill covers every ALLOWED_CAPABILITY_KEYS
        entry, so a key registered by its owning ticket cannot be skipped by
        the drill."""
        code, _, _ = self._call(as_json=True)
        self.assertEqual(code, 0)
        _, stdout, _ = self._call(as_json=True)
        report = json.loads(stdout.getvalue())
        drilled = {cap["key"] for cap in report["capabilities"]}
        self.assertEqual(drilled, set(ALLOWED_CAPABILITY_KEYS))

    def test_new_registry_key_is_drilled_automatically(self):
        """A key added to the registry is picked up by the drill without any
        drill-side change: the drill derives its list from the registry."""
        from unittest.mock import patch

        extended = frozenset(ALLOWED_CAPABILITY_KEYS | {"publication_consumer"})
        with patch(
            "crank.models.monitoring.ALLOWED_CAPABILITY_KEYS", extended
        ), patch(
            "crank.management.commands.rollback_drill.ALLOWED_CAPABILITY_KEYS",
            extended,
        ):
            code, stdout, _ = self._call(as_json=True)
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        drilled = {cap["key"] for cap in report["capabilities"]}
        self.assertIn("publication_consumer", drilled)
        switch = CapabilitySwitch.objects.get(key="publication_consumer")
        self.assertFalse(switch.enabled)

    def test_run_type_matches_key_for_run_type_named_keys(self):
        """Keys that name an AgentRun run type map to themselves."""
        _, stdout, _ = self._call(as_json=True)
        report = json.loads(stdout.getvalue())
        by_key = {cap["key"]: cap for cap in report["capabilities"]}
        for key in ("crawl", "crawl_schedule", "gather_scores", "job_pipeline"):
            self.assertEqual(by_key[key]["run_type"], key)
            self.assertTrue(by_key[key]["run_type_blocked"])

    def test_interactive_agent_drills_the_noop_run_type(self):
        """interactive_agent overrides to the noop run type (settings flags
        are the primary gate for that pairing)."""
        _, stdout, _ = self._call(as_json=True)
        report = json.loads(stdout.getvalue())
        by_key = {cap["key"]: cap for cap in report["capabilities"]}
        self.assertEqual(by_key["interactive_agent"]["run_type"], "noop")

    def test_keys_without_matching_run_type_drill_with_none(self):
        """A registered key without a matching run type (agent_noop) drills
        with run_type=None and run_type_blocked=None; the switch alone is the
        control and this is not a drill failure."""
        code, stdout, _ = self._call(as_json=True)
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        by_key = {cap["key"]: cap for cap in report["capabilities"]}
        self.assertIsNone(by_key["agent_noop"]["run_type"])
        self.assertIsNone(by_key["agent_noop"]["run_type_blocked"])
        self.assertTrue(by_key["agent_noop"]["passed"])

    def test_drill_disabled_switches_cover_every_registered_key(self):
        """After the drill, every registered key has a disabled switch."""
        self._call()
        for key in ALLOWED_CAPABILITY_KEYS:
            switch = CapabilitySwitch.objects.get(key=key)
            self.assertFalse(switch.enabled)
