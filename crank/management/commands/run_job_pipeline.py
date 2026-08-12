# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Management command for the disabled-by-default job pipeline schedule."""

from crank.management.base import AgentRunCommand
from crank.services.job_pipeline import run_job_pipeline


class Command(AgentRunCommand):
    help = "Ingest approved job sources and persist eligible user matches."
    run_type = "job_pipeline"
    enabled_setting = "JOB_PIPELINE_ENABLED"

    def run_payload(self, run, **options):
        return run_job_pipeline(run, **options)
