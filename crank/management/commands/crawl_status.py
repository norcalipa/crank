# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Print per-source listing counts, last crawl time, and last outcome.

Gives operators an at-a-glance inventory health view for every JobSourceCatalog
row, plus a grand total of active listings.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from crank.models.crawl_run import CrawlRun
from crank.models.job import JobListing, JobSourceCatalog


class Command(BaseCommand):
    help = "Print per-source job-listing counts, last crawl time, and last outcome."

    def add_arguments(self, parser):
        parser.add_argument(
            "--include-closed",
            action="store_true",
            help="Include closed and expired listings in counts.",
        )

    def handle(self, *args, **options):
        include_closed = options.get("include_closed", False)
        sources = JobSourceCatalog.objects.all().order_by("name")

        if not sources.exists():
            self.stdout.write(self.style.WARNING("No JobSourceCatalog rows found."))
            self.stdout.write(
                self.style.NOTICE(
                    "Run: python manage.py seed_job_sources --dry-run"
                )
            )
            return 0

        header = (
            f"{'Name':<30} {'Adapter':<20} {'State':<10} {'Enabled':<8} "
            f"{'Listings':>8} {'Last Crawl':<20} {'Last Outcome':<12}"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        total_listings = 0

        for source in sources:
            listing_qs = source.listings.all()
            if not include_closed:
                listing_qs = listing_qs.filter(status=JobListing.Status.ACTIVE)
            listing_count = listing_qs.count()
            total_listings += listing_count

            last_crawl_at = source.last_crawl_at
            last_crawl_str = (
                last_crawl_at.strftime("%Y-%m-%d %H:%M") if last_crawl_at else "never"
            )

            last_run = (
                CrawlRun.objects.filter(job_source=source)
                .order_by("-started_at")
                .first()
            )
            last_outcome = last_run.outcome if last_run else "—"

            self.stdout.write(
                f"{source.name:<30} {source.adapter_key:<20} "
                f"{source.approval_state:<10} {'yes' if source.enabled else 'no':<8} "
                f"{listing_count:>8} {last_crawl_str:<20} {last_outcome:<12}"
            )

        self.stdout.write("-" * len(header))
        self.stdout.write(
            self.style.SUCCESS(f"Total active listings: {total_listings}")
        )
        return 0
