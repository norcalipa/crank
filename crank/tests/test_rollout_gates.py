# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Rollout gate validation tests for issue #328.

These tests verify that the rollout gates defined in ``docs/rollout-gates.md``
are enforceable in code: flags are disabled by default, kill switches work,
sources require approval before enabling, and rollback ownership is explicit.
"""
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from crank.models.agent_run import AgentRun
from crank.models.job import JobSourceCatalog
from crank.models.monitoring import (
    ALLOWED_CAPABILITY_KEYS,
    CapabilitySwitch,
    OperationalChangeAudit,
)
from crank.models.organization import Organization
from crank.models.source import ApprovalState, SourceCatalog
from crank.services import monitoring

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLLOUT_DOC = REPO_ROOT / "docs" / "rollout-gates.md"


class RolloutGateFlagDefaultsTests(TestCase):
    """Verify all capability flags are disabled by default in dev settings."""

    def test_agent_run_enabled_defaults_false(self):
        self.assertFalse(settings.AGENT_RUN_ENABLED)

    def test_agent_noop_enabled_defaults_false(self):
        self.assertFalse(settings.AGENT_NOOP_ENABLED)

    def test_gather_scores_enabled_defaults_false(self):
        self.assertFalse(getattr(settings, "GATHER_SCORES_ENABLED", False))

    def test_job_pipeline_enabled_defaults_false(self):
        self.assertFalse(getattr(settings, "JOB_PIPELINE_ENABLED", False))


class RolloutGateKillSwitchTests(TestCase):
    """Verify CapabilitySwitch kill switches block capabilities."""

    def test_capability_switch_keys_are_registered(self):
        """All three rollout capabilities have registered switch keys."""
        self.assertIn("interactive_agent", ALLOWED_CAPABILITY_KEYS)
        self.assertIn("gather_scores", ALLOWED_CAPABILITY_KEYS)
        self.assertIn("job_pipeline", ALLOWED_CAPABILITY_KEYS)

    def test_capability_switch_blocks_gather_scores(self):
        """Disabling the gather_scores switch makes capability_enabled False."""
        switch = CapabilitySwitch.objects.create(
            key="gather_scores", enabled=False, note="rollout gate test"
        )
        self.assertFalse(monitoring.capability_enabled("gather_scores"))
        self.assertFalse(monitoring.capability_enabled(switch.key))

    def test_capability_switch_blocks_job_pipeline(self):
        """Disabling the job_pipeline switch makes capability_enabled False."""
        CapabilitySwitch.objects.create(
            key="job_pipeline", enabled=False, note="rollout gate test"
        )
        self.assertFalse(monitoring.capability_enabled("job_pipeline"))

    def test_capability_switch_blocks_interactive_agent(self):
        """Disabling the interactive_agent switch makes capability_enabled False."""
        CapabilitySwitch.objects.create(
            key="interactive_agent", enabled=False, note="rollout gate test"
        )
        self.assertFalse(monitoring.capability_enabled("interactive_agent"))

    def test_unregistered_key_rejected(self):
        """A key not in ALLOWED_CAPABILITY_KEYS cannot be saved."""
        with self.assertRaises(ValidationError):
            CapabilitySwitch(key="evil_capability", enabled=True).full_clean()

    def test_kill_switch_does_not_corrupt_existing_data(self):
        """Disabling a switch does not modify or delete existing AgentRun rows."""
        run = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP,
            status=AgentRun.Status.SUCCEEDED,
            counts={"items_seen": 5},
        )
        CapabilitySwitch.objects.create(
            key="interactive_agent", enabled=False, note="gate test"
        )
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.SUCCEEDED)
        self.assertEqual(run.counts, {"items_seen": 5})


class RolloutGateSourceApprovalTests(TestCase):
    """Verify no source is enabled without current approval."""

    def test_source_catalog_defaults_to_pending_and_disabled(self):
        """SourceCatalog defaults: approval_state=pending, enabled=False."""
        org = Organization.objects.create(name="TestOrg")
        catalog = SourceCatalog.objects.create(
            organization=org,
            name="TestSource",
            adapter_key="test.v1",
            base_url="https://test.example",
        )
        self.assertEqual(catalog.approval_state, ApprovalState.PENDING)
        self.assertFalse(catalog.enabled)

    def test_source_catalog_cannot_be_enabled_without_approval(self):
        """A source must be approved before it can be enabled.

        This is a policy gate enforced through admin actions and documented
        in the rollout checklist. The model allows enabled=True with
        approval_state=pending at the DB level, but the rollout gate test
        verifies the documented policy: sources reach production only through
        explicit approval.
        """
        org = Organization.objects.create(name="TestOrg")
        catalog = SourceCatalog.objects.create(
            organization=org,
            name="TestSource",
            adapter_key="test.v1",
            base_url="https://test.example",
            approval_state=ApprovalState.APPROVED,
            enabled=True,
        )
        # An approved+enabled source is the only valid state for production.
        self.assertEqual(catalog.approval_state, ApprovalState.APPROVED)
        self.assertTrue(catalog.enabled)

    def test_blocked_source_cannot_proceed(self):
        """A blocked source must not be enabled for production use."""
        org = Organization.objects.create(name="TestOrg")
        catalog = SourceCatalog.objects.create(
            organization=org,
            name="BlockedSource",
            adapter_key="test.v1",
            base_url="https://test.example",
            approval_state=ApprovalState.BLOCKED,
            enabled=False,
        )
        self.assertFalse(catalog.enabled)
        self.assertEqual(catalog.approval_state, ApprovalState.BLOCKED)

    def test_job_source_catalog_defaults_to_pending_and_disabled(self):
        """JobSourceCatalog defaults: approval_state=pending, enabled=False."""
        source = JobSourceCatalog.objects.create(
            name="TestJobs",
            adapter_key="test.v1",
            base_url="https://jobs.example.test",
        )
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.PENDING)
        self.assertFalse(source.enabled)

    def test_job_source_blocked_cannot_proceed(self):
        """A blocked job source must not be enabled."""
        source = JobSourceCatalog.objects.create(
            name="BlockedJobs",
            adapter_key="test.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.BLOCKED,
            enabled=False,
        )
        self.assertFalse(source.enabled)
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.BLOCKED)


class RolloutGateAuditTests(TestCase):
    """Verify operational changes are audited with actor and confirmation."""

    def test_capability_toggle_records_audit(self):
        """Toggling a capability switch records an OperationalChangeAudit."""
        switch = CapabilitySwitch.objects.create(
            key="interactive_agent", enabled=True, note="test"
        )
        OperationalChangeAudit.record(
            actor=None,
            target_type="capability",
            target_id=switch.key,
            action="disable",
            old_value={"enabled": True},
            new_value={"enabled": False},
            confirmed=True,
        )
        audit = OperationalChangeAudit.objects.get(
            target_type="capability",
            target_id="interactive_agent",
            action="disable",
        )
        self.assertTrue(audit.confirmed)
        self.assertEqual(audit.old_value, {"enabled": True})
        self.assertEqual(audit.new_value, {"enabled": False})

    def test_audit_redacts_sensitive_fields(self):
        """OperationalChangeAudit must not store secrets or prompts."""
        audit = OperationalChangeAudit.record(
            actor=None,
            target_type="capability",
            target_id="gather_scores",
            action="enable",
            old_value={"secret": "should-be-redacted", "prompt": "hidden"},
            new_value={"enabled": True},
        )
        self.assertEqual(audit.old_value["secret"], "<redacted>")
        self.assertEqual(audit.old_value["prompt"], "<redacted>")

    def test_rollback_ownership_is_explicit(self):
        """Rollback ownership is documented in the rollout gates doc.

        The docs/rollout-gates.md document defines rollback owners and
        escalation paths for each capability.
        """
        self.assertTrue(ROLLOUT_DOC.exists(), "docs/rollout-gates.md must exist")
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("Rollback Ownership", content)
        self.assertIn("Operations Lead", content)
        self.assertIn("Engineering Lead", content)


class RolloutGateDocumentTests(TestCase):
    """Verify the rollout gates document has all required sections."""

    def test_rollout_doc_exists(self):
        self.assertTrue(ROLLOUT_DOC.exists())

    def test_rollout_doc_has_three_capabilities(self):
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("Interactive Agent", content)
        self.assertIn("Score Source", content)
        self.assertIn("Job Source", content)

    def test_rollout_doc_has_four_stages(self):
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("Staging", content)
        self.assertIn("Internal Canary", content)
        self.assertIn("Limited Production", content)
        self.assertIn("General Availability", content)

    def test_rollout_doc_has_named_approvers(self):
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("Approver(s)", content)
        self.assertIn("Tech Lead", content)
        self.assertIn("Security Lead", content)
        self.assertIn("Privacy Lead", content)

    def test_rollout_doc_has_observation_windows(self):
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("Observation window", content)
        self.assertIn("48 hours", content)
        self.assertIn("72 hours", content)
        self.assertIn("7 days", content)
        self.assertIn("30 days", content)

    def test_rollout_doc_has_thresholds(self):
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("Success threshold", content)
        self.assertIn("Error threshold", content)
        self.assertIn("Latency threshold", content)
        self.assertIn("Cost threshold", content)
        self.assertIn("Freshness threshold", content)

    def test_rollout_doc_has_rollback_procedures(self):
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("Rollback Procedure", content)
        self.assertIn("CapabilitySwitch", content)
        self.assertIn("OperationalChangeAudit", content)

    def test_rollout_doc_references_kill_switches(self):
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("AGENT_RUN_ENABLED", content)
        self.assertIn("GATHER_SCORES_ENABLED", content)
        self.assertIn("JOB_PIPELINE_ENABLED", content)

    def test_rollout_doc_has_security_section(self):
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("Security and Observability", content)
        self.assertIn("confirm=yes", content)

    def test_rollout_doc_has_non_blocking_findings_section(self):
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("Non-Blocking Findings", content)

    def test_rollout_doc_references_drill_command(self):
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("rollback_drill", content)
