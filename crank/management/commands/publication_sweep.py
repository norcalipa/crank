# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Bounded cron consumer for the publication outbox (issue #470).

Owner: operations cron. Each invocation sweeps at most ``--limit`` (default
``PUBLICATION_SWEEP_BATCH_SIZE``) pending events: deletes each affected cache
key once and marks processed. Gated by ``PUBLICATION_CONSUMER_ENABLED``
(default False) plus the ``publication_consumer`` ``CapabilitySwitch``; when
disabled the command is a successful no-op so the cron schedule can stay
wired while the consumer is off, and pending events simply accumulate.
"""

from django.core.management.base import BaseCommand

from crank.services import monitoring, operations_readiness, publication


class Command(BaseCommand):
    help = (
        "Sweep pending publication events: delete affected cache keys and "
        "mark processed (transactional outbox consumer)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Maximum pending events to sweep "
                "(default: PUBLICATION_SWEEP_BATCH_SIZE)."
            ),
        )

    def handle(self, *args, **options):
        if not publication.consumer_enabled():
            monitoring.record_event("publication_sweep", {"status": "disabled"})
            self.stdout.write("publication consumer disabled; sweep skipped")
            return 0
        try:
            counts = publication.sweep_pending(limit=options["limit"])
        except Exception as exc:
            monitoring.record_event(
                "publication_sweep",
                {
                    "status": "failed",
                    "reason_code": monitoring.failure_reason(exc),
                    "failure_stage": "publication",
                },
            )
            raise
        monitoring.record_event(
            "publication_sweep",
            {
                "status": "completed",
                **counts,
                "outbox_oldest_age_seconds": (
                    operations_readiness.outbox_backlog()["oldest_pending_age_seconds"]
                    or 0
                ),
            },
        )
        self.stdout.write(
            self.style.SUCCESS(
                "publication sweep: scanned={scanned} processed={processed} "
                "keys_deleted={keys_deleted}".format(**counts)
            )
        )
        return 0
