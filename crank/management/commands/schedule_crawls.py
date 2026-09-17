# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Management-command dispatch for bounded, freshness-aware crawls."""

from crank.management.base import AgentRunCommand
from crank.services.crawl_scheduler import PHASE_JOBS, PHASES, plan_crawls


class Command(AgentRunCommand):
    help = "Dispatch bounded crawls for stale approved organization sources."
    run_type = "crawl_schedule"
    enabled_setting = "CRAWL_CRON_ENABLED"

    def add_arguments(self, parser):
        parser.add_argument("--phase", choices=sorted(PHASES), default="all")
        parser.add_argument("--max-sources", type=int, default=None)
        parser.add_argument("--deadline-seconds", type=int, default=None)

    def run_payload(self, run, **options):
        if options["phase"] == PHASE_JOBS:
            # Documented no-op since #462: job-source ingestion has a single
            # owner (run_job_pipeline). Kept accepted so operator scripts and
            # the removed crank-crawl-jobs CronJob command do not hard-fail.
            self.stdout.write(
                "phase 'jobs' is a no-op: job-source ingestion is owned by "
                "run_job_pipeline; use `manage.py run_job_pipeline` or the Job "
                "Retrieval Operations dashboard queue actions."
            )
        return plan_crawls(
            phase=options["phase"],
            max_sources=options.get("max_sources"),
            deadline_seconds=options.get("deadline_seconds"),
        )
