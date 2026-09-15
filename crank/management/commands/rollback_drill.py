# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Rollback drill: verify kill switches stop workflows and data stays consistent.

This command rehearses the rollback procedure documented in
``docs/rollout-gates.md``. It is a diagnostic that:

1. Disables each capability switch and verifies ``monitoring.capability_enabled()``
   returns False.
2. Re-checks the capability gate for the drill run type where it matches the
   switch key (``gather_scores``, ``job_pipeline``); for the
   ``interactive_agent``/``noop`` pairing the settings flags are the primary
   gate and the switch is an additional defense.
3. Checks for orphaned RUNNING runs beyond the stale-lock TTL.
4. Records an ``OperationalChangeAudit`` entry for the drill.
5. Emits a monitoring event for the rollback drill.
6. Reports a JSON or human-readable summary without sensitive data.

The drilled capability list is derived in lockstep from
``ALLOWED_CAPABILITY_KEYS`` (``crank/models/monitoring.py``): every registered
switch key is exercised, so a key added to the registry by its owning ticket
is drilled automatically (enforced by tests).

Scope: the drill does **not** call ``AgentRunCommand.get_enabled()`` and does
**not** snapshot or assert ``AgentRun`` row counts, so it cannot by itself
prove that new runs are blocked; the rollout gate's operator
"Confirm new-run blocking" step covers that guarantee. The drill only reads
``AgentRun`` rows (orphan check) — it does not create or modify them, does
not call external providers, and does not touch source catalogs.
"""
import json
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from crank.models.agent_run import AgentRun
from crank.models.monitoring import (
    ALLOWED_CAPABILITY_KEYS,
    CapabilitySwitch,
    OperationalChangeAudit,
)
from crank.services import monitoring

# RunType override for switches whose key does not name an AgentRun run type.
# interactive_agent's gate is the ``noop`` run type; for that pairing the
# settings flags are the primary gate and the switch is an additional defense
# (documented in docs/rollout-gates.md). Keys without a matching run type
# (existing ``agent_noop`` and future capability-only keys such as
# ``publication_consumer``) drill with ``run_type=None``: the switch itself is
# the control and there is no run-type gate to re-check.
DRILL_RUN_TYPE_OVERRIDES = {
    "interactive_agent": "noop",
}


def drill_capabilities():
    """Derive the drilled capability list from ``ALLOWED_CAPABILITY_KEYS``.

    The drill must exercise every registered switch key in lockstep with the
    registry, so adding a key there automatically extends the drill (the
    registry/drill lockstep is asserted by the rollout-gate tests).
    ``run_type`` is the ``AgentRun.RunType`` whose gate the switch blocks when
    the key names a run type; keys without a matching run type report
    ``run_type=None``.
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
        """Disable a capability switch and verify kill-switch effectiveness."""
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

        # Verify capability_enabled() also returns False for the run_type
        # when the key names a run type (gather_scores, job_pipeline,
        # crawl_schedule, crawl). For interactive_agent/noop the settings
        # flags are the primary gate; the switch is an additional defense.
        # Keys without a matching run type report None (not applicable).
        if run_type is None:
            run_type_blocked = None
        else:
            run_type_blocked = not monitoring.capability_enabled(run_type, default=True)

        passed = cap_blocked
        return {
            "key": key,
            "run_type": run_type,
            "switch_enabled": switch.enabled,
            "capability_blocked": cap_blocked,
            "run_type_blocked": run_type_blocked,
            "passed": passed,
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
            self.stdout.write(
                "    run_type_blocked: {}".format(cap["run_type_blocked"])
            )
        dc = report["data_consistency"]
        self.stdout.write("")
        self.stdout.write("Data consistency:")
        self.stdout.write("  Running runs: {}".format(dc["total_running"]))
        self.stdout.write("  Orphaned (stale): {}".format(dc["orphaned_running"]))
        self.stdout.write("  Stale TTL: {}s".format(dc["stale_ttl_seconds"]))
        self.stdout.write(
            "  Consistency: {}".format("PASSED" if dc["passed"] else "FAILED")
        )
