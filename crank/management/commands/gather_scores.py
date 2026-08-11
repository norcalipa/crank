# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Management command for the disabled-by-default score schedule."""

from crank.management.base import AgentRunCommand
from crank.services.score_gathering import gather_scores


class Command(AgentRunCommand):
    help = "Gather and persist scores from approved, enabled source catalogs."
    run_type = "gather_scores"
    enabled_setting = "GATHER_SCORES_ENABLED"

    def run_payload(self, run, **options):
        return gather_scores(run, **options)
