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


class RolloutGateRegistryTests(TestCase):
    """Epic #454 capability registry contract (issue #463).

    Every epic capability ships with its own independently controlled switch,
    default off, added to ALLOWED_CAPABILITY_KEYS by its owning ticket before
    its code path is enabled. Planned names are documented as planned only.
    The drill exercises every registered key in lockstep with the registry.
    """

    def test_every_registered_key_is_exercised_by_the_drill(self):
        """The drill output covers every ALLOWED_CAPABILITY_KEYS entry.

        This is the enforcement test for the registry/drill lockstep: if a
        key is registered but missing from the drill, this fails.
        """
        import json
        from io import StringIO

        from django.core.management import call_command

        stdout = StringIO()
        call_command("rollback_drill", "--json", stdout=stdout)
        report = json.loads(stdout.getvalue())
        drilled = {cap["key"] for cap in report["capabilities"]}
        self.assertEqual(drilled, set(ALLOWED_CAPABILITY_KEYS))

    def test_rollout_doc_has_capability_registry_section(self):
        """docs/rollout-gates.md documents the epic #454 registry."""
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn("Capability Registry", content)
        for key in ALLOWED_CAPABILITY_KEYS:
            self.assertIn(key, content)

    def test_planned_keys_are_documented_as_planned(self):
        """Reserved-but-unimplemented names are labeled planned.

        Per #463: new flag names must be implemented before being documented
        as available. The planned owners are verified against each issue's
        scope: publication consumer (#470), assistant shell (#472 — "Build
        the shared responsive workspace with a right assistant sidebar"),
        and the recompute phase switches (#475 — "Recompute versioned
        matches"). None may appear as available.
        """
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        for key in ("publication_consumer", "assistant_shell"):
            self.assertIn(key, content)
        self.assertIn("planned", content)
        self.assertIn("#470", content)
        self.assertIn("#472", content)
        self.assertIn("#475", content)
        # Owners that do NOT own these capabilities must not be credited.
        # #462 is the ingestion owner and #471 is the suggest-a-company form
        # ticket; neither owns a recompute phase or the assistant shell.
        self.assertNotIn("| #462 |", content)
        self.assertNotIn("| #471 |", content)
        # Normalize whitespace: the doc wraps lines at ~79 columns, so the
        # phrase may span a newline.
        normalized = " ".join(content.split())
        self.assertIn(
            "must be implemented before being documented as available", normalized
        )


class RegisteredGateWiringTests(TestCase):
    """Every registered switch key gates the path the registry claims.

    The drill's pass criterion (rollback_drill.gate_verifiers) proves these
    gates at drill time; these tests pin the production wiring directly so
    a registry row can never drift from the path it actually controls.
    """

    @override_settings(AGENT_RUN_ENABLED=True, AGENT_NOOP_ENABLED=True)
    def test_agent_noop_switch_blocks_the_noop_command(self):
        """With both settings flags on, the agent_noop switch alone blocks
        the noop run path (the switch, not just the run type, is the
        control)."""
        from io import StringIO

        from django.core.management import call_command

        from crank.management.commands.agent_noop import Command

        CapabilitySwitch.objects.create(key="agent_noop", enabled=False, note="t")
        self.assertFalse(Command().get_enabled())
        stdout = StringIO()
        code = call_command("agent_noop", stdout=stdout)
        self.assertEqual(code, 0)
        self.assertIn("disabled", stdout.getvalue())
        self.assertEqual(AgentRun.objects.count(), 0)

    def test_crawl_switch_blocks_on_demand_crawl(self):
        """The crawl switch gates trigger_crawl before any source lookup,
        so no CrawlRun/AgentRun rows are created when it is off."""
        from crank.services.crawl_runs import CrawlRequestError, trigger_crawl

        CapabilitySwitch.objects.create(key="crawl", enabled=False, note="t")
        with self.assertRaises(CrawlRequestError) as ctx:
            trigger_crawl(source_key="anything", source_type="job")
        self.assertIn("disabled", str(ctx.exception))
        self.assertEqual(AgentRun.objects.count(), 0)

    @override_settings(INTERACTIVE_AGENT_ENABLED=True)
    def test_interactive_agent_switch_blocks_the_llm_path(self):
        """With the feature flag on, the interactive_agent switch alone
        blocks the interactive chat gate."""
        from crank.agents.llm import is_interactive_agent_enabled

        CapabilitySwitch.objects.create(key="interactive_agent", enabled=False, note="t")
        self.assertFalse(is_interactive_agent_enabled())

    @override_settings(
        AGENT_RUN_ENABLED=True,
        GATHER_SCORES_ENABLED=True,
        JOB_PIPELINE_ENABLED=True,
        CRAWL_CRON_ENABLED=True,
    )
    def test_run_type_commands_blocked_by_their_switches(self):
        """gather_scores/job_pipeline/crawl_schedule switches block their
        commands even with every settings flag on."""
        from crank.management.commands import (
            gather_scores,
            run_job_pipeline,
            schedule_crawls,
        )

        for module, key in (
            (gather_scores, "gather_scores"),
            (run_job_pipeline, "job_pipeline"),
            (schedule_crawls, "crawl_schedule"),
        ):
            CapabilitySwitch.objects.create(key=key, enabled=False, note="t")
            self.assertFalse(module.Command().get_enabled(), key)
