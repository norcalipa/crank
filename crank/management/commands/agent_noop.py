# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Reference AgentRun command: does no external work.

Disabled by default (``AGENT_NOOP_ENABLED``). When enabled it exercises the
full idempotent run lifecycle (claim -> finalize) and the Kubernetes CronJob
wiring end to end, recording a run and emitting New Relic events.
"""
from crank.management.base import AgentRunCommand


class Command(AgentRunCommand):
    help = (
        "No-op agent run. Reference command for the idempotent AgentRun "
        "lifecycle and the disabled-by-default Kubernetes CronJob foundation."
    )

    run_type = "noop"
    enabled_setting = "AGENT_NOOP_ENABLED"

    def run_payload(self, run, **options):
        # No external work. Report zero outcomes so the run has meaningful
        # counts rather than an empty dict.
        return {
            "items_seen": 0,
            "items_created": 0,
            "items_updated": 0,
            "items_failed": 0,
        }