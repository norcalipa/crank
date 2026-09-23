# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Management-command lifecycle tests for the recompute_matches drain (issue #475)."""

from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from crank.models import AgentRun
from crank.models.preference import UserPreference


class RecomputeMatchesCommandTests(TestCase):
    def call(self, *args):
        stdout = StringIO()
        stderr = StringIO()
        code = call_command("recompute_matches", *args, stdout=stdout, stderr=stderr)
        return code, stdout, stderr

    @override_settings(MATCH_RECOMPUTE_ENABLED=False)
    def test_disabled_schedule_does_no_work(self):
        code, stdout, _ = self.call()
        self.assertEqual(code, 0)
        self.assertFalse(AgentRun.objects.exists())
        self.assertIn("disabled", stdout.getvalue())

    @override_settings(AGENT_RUN_ENABLED=True, MATCH_RECOMPUTE_ENABLED=True)
    def test_enabled_no_users_succeeds_with_zero_counts(self):
        code, stdout, _ = self.call()
        self.assertEqual(code, 0)
        run = AgentRun.objects.get(run_type=AgentRun.RunType.MATCH_RECOMPUTE)
        self.assertEqual(run.status, AgentRun.Status.SUCCEEDED)
        self.assertEqual(run.counts["users_total"], 0)
        self.assertIn("succeeded", stdout.getvalue())

    @override_settings(AGENT_RUN_ENABLED=True, MATCH_RECOMPUTE_ENABLED=True)
    def test_processes_pending_users_and_records_counts(self):
        user = User.objects.create_user("owner", password="secret")
        UserPreference.objects.create(user=user, revision=0)
        code, stdout, _ = self.call()
        self.assertEqual(code, 0)
        run = AgentRun.objects.get(run_type=AgentRun.RunType.MATCH_RECOMPUTE)
        self.assertEqual(run.status, AgentRun.Status.SUCCEEDED)
        self.assertEqual(run.counts["users_total"], 1)
        self.assertEqual(run.counts["users_succeeded"], 1)

    @override_settings(AGENT_RUN_ENABLED=True, MATCH_RECOMPUTE_ENABLED=True)
    def test_dry_run_writes_nothing_and_claims_no_run(self):
        user = User.objects.create_user("owner", password="secret")
        UserPreference.objects.create(user=user, revision=0)
        code, stdout, _ = self.call("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(AgentRun.objects.exists())
        # Per-tier counts (issue #475 review round 2, MINOR finding 5): a
        # single combined total can't verify the rollout gate
        # (preference_dirty == 0) or distinguish generation-dirty reasons.
        output = stdout.getvalue()
        self.assertIn("preference_dirty=1", output)
        self.assertIn("generation_dirty_data_stale=0", output)
        self.assertIn("generation_dirty_version_mismatch=0", output)
        self.assertIn("generation_dirty_interrupted=0", output)
        self.assertIn("generation_dirty_age_stale=0", output)
        self.assertIn("total_capped_by_one_drain=1", output)

    @override_settings(AGENT_RUN_ENABLED=True, MATCH_RECOMPUTE_ENABLED=True)
    def test_dry_run_disabled_still_reports_and_claims_nothing(self):
        code, stdout, _ = self.call("--dry-run", "--limit", "5")
        self.assertEqual(code, 0)
        self.assertFalse(AgentRun.objects.exists())
        self.assertIn("limit=5", stdout.getvalue())

    @override_settings(AGENT_RUN_ENABLED=True, MATCH_RECOMPUTE_ENABLED=True)
    def test_limit_option_is_forwarded(self):
        with patch(
            "crank.management.commands.recompute_matches.match_recompute.drain",
            return_value={
                "users_total": 0,
                "users_succeeded": 0,
                "users_failed": 0,
                "matches_persisted": 0,
                "stale_discarded": 0,
                "duplicate_skipped": 0,
                "deadline_reached": False,
            },
        ) as drain:
            self.call("--limit", "7")
        self.assertEqual(drain.call_args.args[0], 7)

    @override_settings(AGENT_RUN_ENABLED=True, MATCH_RECOMPUTE_ENABLED=True)
    def test_failure_raises_command_error_and_records_failed_run(self):
        with patch(
            "crank.management.commands.recompute_matches.match_recompute.drain",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(CommandError):
                self.call()
        run = AgentRun.objects.get(run_type=AgentRun.RunType.MATCH_RECOMPUTE)
        self.assertEqual(run.status, AgentRun.Status.FAILED)
