# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Read-only inventory health probe for the production job-source bootstrap.

Exits non-zero when the inventory is unhealthy so a Kubernetes CronJob or
external monitor can surface it, and emits a bounded ``inventory_health``
New Relic event for dashboard/alert queries. No credentials or provider calls
are made, so the command is safe to run on any schedule, including before the
crawl is ever enabled.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from crank.services import inventory_health, monitoring


class Command(BaseCommand):
    help = "Check job-source inventory health and emit a bounded telemetry event."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-emit",
            action="store_true",
            help="Skip the New Relic telemetry event (useful for local checks).",
        )

    def handle(self, *args, **options):
        result = inventory_health.check_inventory_health()

        self.stdout.write(
            self.style.SUCCESS(
                "job inventory health: "
                f"sources_total={result['sources_total']} "
                f"enabled_sources={result['enabled_sources']} "
                f"active_listings={result['active_listings']} "
                f"stale_sources={result['stale_sources']} "
                f"repeated_failure_sources={result['repeated_failure_sources']} "
                f"collapsed_sources={result['collapsed_sources']} "
                f"unregistered_adapter_sources={result['unregistered_adapter_sources']}"
            )
        )

        if result["violations"]:
            for violation in result["violations"]:
                self.stdout.write(self.style.ERROR(f"UNHEALTHY: {violation}"))
        else:
            self.stdout.write(self.style.SUCCESS("healthy"))

        if not options["no_emit"]:
            monitoring.record_event("inventory_health", result)

        if result["healthy"]:
            return
        sys.exit(1)
