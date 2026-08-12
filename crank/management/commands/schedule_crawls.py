# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Management-command dispatch for bounded, freshness-aware crawls."""

from crank.management.base import AgentRunCommand
from crank.services.crawl_scheduler import PHASES, plan_crawls


class Command(AgentRunCommand):
    help = "Dispatch bounded crawls for stale approved sources."
    run_type = "crawl_schedule"
    enabled_setting = "CRAWL_CRON_ENABLED"

    def add_arguments(self, parser):
        parser.add_argument("--phase", choices=sorted(PHASES), default="all")
        parser.add_argument("--max-sources", type=int, default=None)
        parser.add_argument("--deadline-seconds", type=int, default=None)

    def run_payload(self, run, **options):
        return plan_crawls(
            phase=options["phase"],
            max_sources=options.get("max_sources"),
            deadline_seconds=options.get("deadline_seconds"),
        )
