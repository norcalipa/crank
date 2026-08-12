# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Run one bounded, audited crawl for an approved source."""

from django.core.management.base import BaseCommand, CommandError

from crank.services.crawl_runs import CrawlRequestError, trigger_crawl


class Command(BaseCommand):
    help = "Trigger one bounded crawl for an approved source."

    def add_arguments(self, parser):
        parser.add_argument("--source-key", required=True)
        parser.add_argument("--source-type", choices=["organization", "job"], required=True)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("refusing to crawl without --confirm")
        try:
            run = trigger_crawl(
                source_key=options["source_key"],
                source_type=options["source_type"],
            )
        except CrawlRequestError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"crawl {run.pk}: {run.source_key} {run.outcome}"))
        return 0
