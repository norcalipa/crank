# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Bounded drain for durable job-match recomputation (issue #475).

Owner: operations cron (``deploy/cronjob-match-recompute.yaml``, suspended
by default). Each invocation processes at most ``--limit``
(``MATCH_RECOMPUTE_MAX_USERS``) pending users until
``MATCH_RECOMPUTE_DEADLINE_SECONDS``. Gated by ``AGENT_RUN_ENABLED`` +
``MATCH_RECOMPUTE_ENABLED`` plus the ``match_recompute`` ``CapabilitySwitch``
(via ``AgentRunCommand.get_enabled()``). ``--dry-run`` prints pending counts
by tier and exits before claiming a run, so it creates no ``AgentRun`` row.
"""
import time

from crank.management.base import AgentRunCommand
from crank.services import agent_runs, match_recompute
from crank.services.monitoring import EVENT_NAMES


class Command(AgentRunCommand):
    help = "Recompute and publish committed job-match generations for dirty users."
    run_type = "match_recompute"
    enabled_setting = "MATCH_RECOMPUTE_ENABLED"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum users to process (default: MATCH_RECOMPUTE_MAX_USERS).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print pending counts by tier and exit; claims no AgentRun.",
        )

    def _limit(self, options):
        from django.conf import settings

        if options.get("limit"):
            return max(1, int(options["limit"]))
        return max(1, int(getattr(settings, "MATCH_RECOMPUTE_MAX_USERS", 50)))

    def handle(self, *args, **options):
        if options.get("dry_run"):
            limit = self._limit(options)
            # Per-tier, unbounded-by-limit counts (issue #475 review round
            # 2, MINOR finding 5): a single capped total can't show the
            # rollout gate (preference_dirty == 0) or tell data/ranker/
            # interrupted/age generation-dirtiness apart. Tiers overlap, so
            # they intentionally don't sum to total_capped.
            counts = match_recompute.pending_counts(limit)
            self.stdout.write(
                "match_recompute --dry-run: "
                f"preference_dirty={counts['preference_dirty']} "
                f"generation_dirty_data_stale={counts['generation_dirty_data_stale']} "
                f"generation_dirty_version_mismatch={counts['generation_dirty_version_mismatch']} "
                f"generation_dirty_interrupted={counts['generation_dirty_interrupted']} "
                f"generation_dirty_age_stale={counts['generation_dirty_age_stale']} "
                f"(limit={counts['limit']}, "
                f"total_capped_by_one_drain={counts['total_capped']}, "
                "tiers overlap and do not sum to total_capped); "
                "no run claimed, nothing written"
            )
            return 0
        return super().handle(*args, **options)

    def run_payload(self, run, **options):
        from django.conf import settings

        limit = self._limit(options)
        deadline_seconds = max(
            0.0, float(getattr(settings, "MATCH_RECOMPUTE_DEADLINE_SECONDS", 240))
        )
        deadline = time.monotonic() + deadline_seconds
        counts = match_recompute.drain(limit, deadline=deadline)
        if "matching_batch" in EVENT_NAMES:
            agent_runs.monitoring.record_event(
                "matching_batch",
                {
                    **counts,
                    "stage": "match_recompute",
                    "status": "deadline" if counts["deadline_reached"] else "completed",
                    "reason_code": "deadline" if counts["deadline_reached"] else "none",
                },
            )
        return counts
