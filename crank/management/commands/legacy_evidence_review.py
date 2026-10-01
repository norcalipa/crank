# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""List (and optionally queue for review) unreviewed legacy field evidence (#474)."""

from django.core.management.base import BaseCommand

from crank.models.monitoring import OperationalChangeAudit
from crank.services import company_evidence


class Command(BaseCommand):
    help = (
        "List accepted RTO, funding, public-status and accelerated-vesting rows "
        "that no person reviewed (auto-verified by crawls before #474). With "
        "--queue, open a pending claim for each so staff can accept or reject "
        "it without waiting for a recrawl. Rows stay in effect until staff decide."
    )

    def add_arguments(self, parser):
        parser.add_argument("--queue", action="store_true", help="open pending claims")

    def handle(self, *args, **options):
        clean = company_evidence.strip_unsafe_characters
        rows = company_evidence.legacy_unreviewed_rows()
        queued = []
        for row in rows:
            # Legacy values predate sanitising at write time: never print them raw.
            self.stdout.write(
                f"{row.organization_id}\t{clean(row.organization.name)}\t{row.field_key}\t"
                f"{row.pk}\t{clean(row.value_text)}"
            )
            if options["queue"]:
                claim = company_evidence.queue_legacy_claim(row)
                if claim is not None:
                    queued.append(claim.pk)
        if options["queue"]:
            OperationalChangeAudit.record(
                actor=None,
                target_type="legacy_evidence",
                target_id="queue",
                action="legacy_queue",
                old_value={"legacy_rows": len(rows)},
                new_value={"claims_queued": len(queued), "claim_ids": queued[:200]},
                confirmed=False,
            )
        self.stdout.write(f"{len(rows)} legacy unreviewed row(s); {len(queued)} claim(s) queued.")
