# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Rollback drill: verify kill switches stop workflows and data stays consistent.

This command rehearses the rollback procedure documented in
``docs/rollout-gates.md``. It is a diagnostic that:

1. Disables each capability switch and verifies the switch reads back
   disabled (``monitoring.capability_enabled()``).
2. Invokes the **real execution gate** that each registered key's
   production path consults (``gate_verifiers()``), with the settings
   flags forced on so the switch is the only variable, and requires that
   gate to block. ``passed`` requires both checks: a registered key with
   no mapped real gate, or whose real gate does not block, **fails** the
   drill instead of reporting success.
3. Checks for orphaned RUNNING runs beyond the stale-lock TTL.
4. Records an ``OperationalChangeAudit`` entry for the drill.
5. Emits a monitoring event for the rollback drill.
6. Reports a JSON or human-readable summary without sensitive data.

The drilled capability list is derived in lockstep from
``ALLOWED_CAPABILITY_KEYS`` (``crank/models/monitoring.py``): every registered
switch key is exercised, so a key added to the registry by its owning ticket
is drilled automatically (enforced by tests).

Scope: the drill invokes gate functions (pure settings/switch readers such
as ``AgentRunCommand.get_enabled()`` and ``crawl_runs.crawl_enabled()``)
but never runs command payloads, never creates or modifies ``AgentRun``
rows (the orphan check only reads them), does not call external providers,
and does not touch source catalogs. End-to-end "no new runs are created"
remains the rollout gate's operator "Confirm new-run blocking" step.
"""
import json
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.test import override_settings
from django.utils import timezone

from crank.models.agent_run import AgentRun
from crank.models.monitoring import (
    ALLOWED_CAPABILITY_KEYS,
    CapabilitySwitch,
    OperationalChangeAudit,
)
from crank.services import monitoring

# Informational ``AgentRun.RunType`` mapping for switches whose key does not
# name a run type. ``agent_noop`` commands the ``noop`` run type (its
# ``AgentRunCommand`` claims and records that type); ``interactive_agent`` is
# not an agent-run command at all (its gate is the interactive-LLM path,
# ``crank/agents/llm.py``), so it reports ``run_type=None``. The pass
# criterion never depends on this mapping — it depends on the real-gate
# verification in ``gate_verifiers()``.
DRILL_RUN_TYPE_OVERRIDES = {
    "agent_noop": "noop",
}

# Settings flags forced on while the drill verifies each real execution
# gate, so the disabled ``CapabilitySwitch`` row is the only variable that
# can block the path. If the switch alone blocks the path, the rollback
# control is real; if the path stays open with the switch off, the drill
# fails the key.
GATE_FORCED_SETTINGS = {
    "AGENT_RUN_ENABLED": True,
    "AGENT_NOOP_ENABLED": True,
    "GATHER_SCORES_ENABLED": True,
    "JOB_PIPELINE_ENABLED": True,
    "CRAWL_CRON_ENABLED": True,
    "INTERACTIVE_AGENT_ENABLED": True,
}


def gate_verifiers():
    """Map each registered switch key to its **real** production gate.

    Each verifier is the actual function the capability's execution path
    consults — never a drill-side reimplementation — so a switch row that no
    path reads cannot pass the drill:
    ``interactive_agent`` → ``crank.agents.llm.is_interactive_agent_enabled``
    (the interactive chat path); ``gather_scores``/``job_pipeline``/
    ``crawl_schedule``/``agent_noop`` → their ``AgentRunCommand.get_enabled()``
    (the scheduled-run path, keyed by the registry key); ``crawl`` →
    ``crank.services.crawl_runs.crawl_enabled`` (the on-demand crawl path).

    A key registered in ``ALLOWED_CAPABILITY_KEYS`` without an entry here
    fails the drill with ``reason="no real-gate verifier registered"``: the
    owning ticket must wire (and map) the gate together with the registry
    key, keeping registry rows aligned with the paths they actually control.
    """
    from crank.agents import llm as llm_gates
    from crank.management.commands import (
        agent_noop,
        gather_scores,
        run_job_pipeline,
        schedule_crawls,
    )
    from crank.services import crawl_runs

    return {
        "interactive_agent": llm_gates.is_interactive_agent_enabled,
        "gather_scores": gather_scores.Command().get_enabled,
        "job_pipeline": run_job_pipeline.Command().get_enabled,
        "agent_noop": agent_noop.Command().get_enabled,
        "crawl_schedule": schedule_crawls.Command().get_enabled,
        "crawl": crawl_runs.crawl_enabled,
    }


def drill_capabilities():
    """Derive the drilled capability list from ``ALLOWED_CAPABILITY_KEYS``.

    The drill must exercise every registered switch key in lockstep with the
    registry, so adding a key there automatically extends the drill (the
    registry/drill lockstep is asserted by the rollout-gate tests).
    ``run_type`` is informational: the ``AgentRun.RunType`` the capability's
    path exercises when the key names one (or is overridden to one); keys
    that are not agent-run commands report ``run_type=None``. The pass
    criterion is the real-gate verification, not this mapping.
    """
    run_type_values = set(AgentRun.RunType.values)
    capabilities = []
    for key in sorted(ALLOWED_CAPABILITY_KEYS):
        run_type = DRILL_RUN_TYPE_OVERRIDES.get(key, key)
        if run_type not in run_type_values:
            run_type = None
        capabilities.append({"key": key, "run_type": run_type})
    return capabilities


STALE_TTL_SECONDS = getattr(settings, "AGENT_RUN_STALE_AFTER_SECONDS", 3600)


class Command(BaseCommand):
    help = (
        "Rehearse the rollback procedure: disable capability switches, "
        "verify the kill-switch capability gates respond, and check data "
        "consistency."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit a machine-readable JSON report.",
        )

    def handle(self, *args, **options):
        from django.core.management.base import CommandError

        as_json = options.get("as_json", False)
        report = self._run_drill()

        if as_json:
            self.stdout.write(json.dumps(report, sort_keys=True))
        else:
            self._write_human_report(report)

        if report["status"] != "passed":
            raise CommandError("Rollback drill failed; see report above.")
        return 0

    def _run_drill(self):
        """Execute the rollback drill and return a report dict."""
        capabilities = drill_capabilities()
        results = []
        overall_passed = True

        for cap in capabilities:
            result = self._drill_capability(cap)
            results.append(result)
            if not result["passed"]:
                overall_passed = False

        consistency = self._check_data_consistency()
        if not consistency["passed"]:
            overall_passed = False

        OperationalChangeAudit.record(
            actor=None,
            target_type="rollback_drill",
            target_id="staging",
            action="rollback_drill",
            old_value={},
            new_value={
                "capabilities_drilled": [r["key"] for r in results],
                "overall_passed": overall_passed,
            },
            confirmed=True,
        )

        monitoring.record_event(
            "operational_change",
            {
                "action": "rollback_drill",
                "capability": "all",
                "confirmed": True,
            },
        )

        return {
            "status": "passed" if overall_passed else "failed",
            "capabilities": results,
            "data_consistency": consistency,
            "drilled_at": timezone.now().isoformat(),
        }

    def _drill_capability(self, cap):
        """Disable a capability switch and verify its real gate blocks."""
        key = cap["key"]
        run_type = cap["run_type"]

        switch, created = CapabilitySwitch.objects.get_or_create(
            key=key, defaults={"enabled": False, "note": "rollback drill"}
        )
        if not created and switch.enabled:
            switch.enabled = False
            switch.note = "rollback drill"
            switch.save(update_fields=["enabled", "note", "modified"])

        # Verify capability_enabled() returns False for the switch key.
        cap_enabled = monitoring.capability_enabled(key, default=True)
        cap_blocked = not cap_enabled

        # Verify the REAL execution gate the capability's path consults
        # blocks with the switch disabled. The settings flags are forced on
        # (GATE_FORCED_SETTINGS) so the switch is the only variable: if the
        # gate still allows the path, the switch does not control it and the
        # drill fails the key instead of reporting a successful rollback
        # control. A key with no mapped verifier cannot report passed.
        verifier = gate_verifiers().get(key)
        reason = None
        if verifier is None:
            gate_blocked = None
            reason = "no real-gate verifier registered"
        else:
            with override_settings(**GATE_FORCED_SETTINGS):
                try:
                    gate_blocked = not bool(verifier())
                except Exception as exc:  # defensive: report, never crash
                    gate_blocked = False
                    reason = "real gate raised {}: {}".format(
                        type(exc).__name__, str(exc)[:80]
                    )
            if not gate_blocked and reason is None:
                reason = "real gate did not block with the switch disabled"

        passed = bool(cap_blocked and gate_blocked)
        if not passed and reason is None:
            reason = "switch row did not read back as disabled"
        return {
            "key": key,
            "run_type": run_type,
            "switch_enabled": switch.enabled,
            "capability_blocked": cap_blocked,
            "gate_blocked": gate_blocked,
            "passed": passed,
            "reason": reason,
        }

    def _check_data_consistency(self):
        """Verify no orphaned RUNNING runs beyond the stale-lock TTL."""
        stale_cutoff = timezone.now() - timedelta(seconds=STALE_TTL_SECONDS)
        running_runs = AgentRun.objects.filter(status=AgentRun.Status.RUNNING)
        orphaned = running_runs.filter(started_at__lt=stale_cutoff)
        orphaned_count = orphaned.count()
        total_running = running_runs.count()

        return {
            "total_running": total_running,
            "orphaned_running": orphaned_count,
            "stale_ttl_seconds": STALE_TTL_SECONDS,
            "passed": orphaned_count == 0,
        }

    def _write_human_report(self, report):
        self.stdout.write("Rollback drill: {}".format(report["status"]))
        self.stdout.write("Drilled at: {}".format(report["drilled_at"]))
        self.stdout.write("")
        self.stdout.write("Capabilities:")
        for cap in report["capabilities"]:
            self.stdout.write(
                "  {}: {} (switch={})".format(
                    cap["key"],
                    "PASSED" if cap["passed"] else "FAILED",
                    "off" if not cap["switch_enabled"] else "on",
                )
            )
            self.stdout.write(
                "    capability_blocked: {}".format(cap["capability_blocked"])
            )
            self.stdout.write("    gate_blocked: {}".format(cap["gate_blocked"]))
            if cap.get("reason"):
                self.stdout.write("    reason: {}".format(cap["reason"]))
        dc = report["data_consistency"]
        self.stdout.write("")
        self.stdout.write("Data consistency:")
        self.stdout.write("  Running runs: {}".format(dc["total_running"]))
        self.stdout.write("  Orphaned (stale): {}".format(dc["orphaned_running"]))
        self.stdout.write("  Stale TTL: {}s".format(dc["stale_ttl_seconds"]))
        self.stdout.write(
            "  Consistency: {}".format("PASSED" if dc["passed"] else "FAILED")
        )
