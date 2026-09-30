# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""List (and optionally queue for review) unreviewed legacy field evidence (#474)."""

from django.core.management.base import BaseCommand

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
        rows = company_evidence.legacy_unreviewed_rows()
        queued = 0
        for row in rows:
            self.stdout.write(
                f"{row.organization_id}\t{row.organization.name}\t{row.field_key}\t"
                f"{row.pk}\t{row.value_text}"
            )
            if options["queue"] and company_evidence.queue_legacy_claim(row):
                queued += 1
        self.stdout.write(f"{len(rows)} legacy unreviewed row(s); {queued} claim(s) queued.")
