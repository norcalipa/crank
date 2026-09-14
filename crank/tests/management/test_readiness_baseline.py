# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the readiness-baseline command and staging fixture loader (issue #455).

Covers the required baseline record shape, the three distinguishable failure
conditions (disabled provider/source, missing schema, failed completed run),
secret redaction, ``--out`` file writing, and the staging fixture smoke load.
"""

import io
import json
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from crank.management.commands.readiness_baseline import baseline_record
from crank.models.agent_run import AgentRun
from crank.models.company_profile import CompanyProfileObservation
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.score import Score

SECRET_A = "x" * 40
SECRET_B = "u" * 32
SECRET_C = "f" * 24


def _run(*args):
    stdout = io.StringIO()
    stderr = io.StringIO()
    call_command("readiness_baseline", *args, stdout=stdout, stderr=stderr, no_color=True)
    return stdout.getvalue(), stderr.getvalue()


def _parse(payload: str) -> dict:
    # The command may append a styled notice after the JSON payload; JSON
    # itself is emitted first and is a single pretty-printed document.
    return json.loads(payload)


class ReadinessBaselineCommandTests(TestCase):
    def test_required_keys_present(self):
        payload, _ = _run()
        record = _parse(payload)
        for key in (
            "generated_at",
            "env",
            "source_version",
            "frontend_build_id",
            "migrations",
            "job_search_provider",
            "capabilities",
            "inventory",
            "latest_runs",
            "source_counts",
        ):
            self.assertIn(key, record)
        self.assertEqual(
            set(record["migrations"].keys()),
            {"applied_count", "pending_count", "status"},
        )
        self.assertEqual(
            set(record["source_counts"].keys()),
            {"configured", "approved", "enabled"},
        )

    def test_disabled_provider_and_source_are_distinguishable(self):
        JobSourceCatalog.objects.create(
            name="Pending Source",
            adapter_key="usajobs",
            base_url="https://data.usajobs.gov/",
        )
        payload, _ = _run()
        record = _parse(payload)
        # (a) Disabled provider: named capability flag, distinct field.
        interactive = next(
            capability
            for capability in record["capabilities"]["capabilities"]
            if capability["name"] == "interactive_agent"
        )
        self.assertIn("enabled", interactive)
        self.assertIn("issues", interactive)
        self.assertEqual(record["capabilities"]["capabilities"][0]["enabled"], False)
        self.assertEqual(
            (record["source_counts"]["approved"], record["source_counts"]["enabled"]),
            (0, 0),
        )
        self.assertEqual(record["source_counts"]["configured"], 1)

    def test_missing_schema_is_distinguishable(self):
        fake_summary = {"applied_count": 5, "pending_count": 2, "status": "pending"}
        with mock.patch(
            "crank.management.commands.readiness_baseline.migration_status_summary",
            return_value=fake_summary,
        ):
            payload, _ = _run()
        record = _parse(payload)
        # (b) Missing schema: its own named field, independent of sources/runs.
        self.assertEqual(record["migrations"]["status"], "pending")
        self.assertEqual(record["migrations"]["pending_count"], 2)
        self.assertIn("source_counts", record)
        self.assertIn("latest_runs", record)

    def test_failed_completed_run_is_distinguishable(self):
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
            error_summary="pipeline exploded after 3 attempts",
        )
        AgentRun.objects.create(
            run_type=AgentRun.RunType.GATHER_SCORES,
            status=AgentRun.Status.RUNNING,
        )
        payload, _ = _run()
        record = _parse(payload)
        by_type = {run["run_type"]: run for run in record["latest_runs"]}
        # (c) Failed completed run: terminal status plus sanitized summary.
        self.assertEqual(by_type["job_pipeline"]["status"], "failed")
        self.assertTrue(by_type["job_pipeline"]["terminal"])
        self.assertIn(
            "pipeline exploded after 3 attempts",
            by_type["job_pipeline"]["error_summary"],
        )
        # Non-terminal latest run is reported but marked not terminal.
        self.assertEqual(by_type["gather_scores"]["status"], "running")
        self.assertFalse(by_type["gather_scores"]["terminal"])
        # Run types with no runs are simply absent.
        self.assertNotIn("crawl", by_type)

    @override_settings(
        LLM_API_KEY=SECRET_A,
        USAJOBS_AUTH_KEY=SECRET_B,
        FIRECRAWL_API_KEY=SECRET_C,
        SECRET_KEY="s" * 50,
    )
    def test_secret_values_never_appear_in_output(self):
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
            error_summary=(
                f"connection refused with key {SECRET_A} "
                f"and auth {SECRET_B} / {SECRET_C}"
            ),
        )
        payload, _ = _run()
        for secret in (SECRET_A, SECRET_B, SECRET_C, "s" * 50):
            self.assertNotIn(secret, payload)
        record = _parse(payload)
        by_type = {run["run_type"]: run for run in record["latest_runs"]}
        self.assertIn("[redacted]", by_type["job_pipeline"]["error_summary"])

    def test_out_option_writes_file_with_trailing_newline(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as handle:
            stdout, _ = _run("--out", handle.name)
            with open(handle.name, "r", encoding="utf-8") as written:
                file_payload = written.read()
        self.assertTrue(file_payload.endswith("\n"))
        self.assertEqual(_parse(file_payload), _parse(stdout))

    def test_baseline_record_helper_matches_command_output(self):
        payload, _ = _run()
        emitted = _parse(payload)
        helper = baseline_record()
        # generated_at is a wall-clock capture time; everything else must match.
        emitted.pop("generated_at")
        helper.pop("generated_at")
        self.assertEqual(emitted, helper)


class SeedStagingBaselineTests(TestCase):
    def _seed(self, *args):
        stdout = io.StringIO()
        call_command("seed_staging_baseline", *args, stdout=stdout, no_color=True)
        return stdout.getvalue()

    def test_fixture_smoke_load_and_baseline(self):
        output = self._seed()
        self.assertIn("Staging baseline fixtures ready", output)

        # Scores: one active (status 1) + one superseded (status 0).
        self.assertEqual(Score.objects.count(), 2)
        self.assertEqual(
            Score.objects.filter(status=1).count(),
            1,
            "expected exactly one active score",
        )
        self.assertEqual(
            Score.objects.filter(status=0).count(),
            1,
            "expected exactly one superseded score",
        )

        # Company evidence: accepted / stale / conflicted.
        observations = CompanyProfileObservation.objects.all()
        self.assertEqual(observations.count(), 3)
        self.assertEqual(
            set(observations.values_list("status", flat=True)),
            {
                CompanyProfileObservation.Status.ACCEPTED,
                CompanyProfileObservation.Status.CONFLICTED,
            },
        )
        observed_ats = sorted(observations.values_list("observed_at", flat=True))
        self.assertLess(
            observed_ats[0],
            observed_ats[-1],
            "expected one stale (older) and one fresh observation",
        )

        # Jobs: one active + one expired listing under an approved+enabled source.
        source = JobSourceCatalog.objects.get(name="Staging Baseline Source")
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.APPROVED)
        self.assertTrue(source.enabled)
        self.assertEqual(
            JobListing.objects.filter(status=JobListing.Status.ACTIVE).count(), 1
        )
        self.assertEqual(
            JobListing.all_objects.filter(status=JobListing.Status.EXPIRED).count(),
            1,
        )

        # Matches: ordinary case + unknown-requirement case for the test user.
        user = get_user_model().objects.get(username="staging_user")
        matches = JobMatch.objects.filter(user=user)
        self.assertEqual(matches.count(), 2)
        factor_names = {
            factor["factor"]
            for match in matches
            for factor in match.factors
        }
        self.assertIn("requirement:quantum_fluency", factor_names)
        self.assertIn("organization_scores", factor_names)

        # Accounts: ordinary + staff, both with the requested password.
        staff = get_user_model().objects.get(username="staging_staff")
        self.assertFalse(user.is_staff)
        self.assertTrue(staff.is_staff)
        self.assertTrue(user.check_password("staging-baseline-throwaway"))
        self.assertTrue(staff.check_password("staging-baseline-throwaway"))

        # The baseline command runs against the loaded fixtures and stays JSON-safe.
        payload, _ = _run()
        record = _parse(payload)
        self.assertGreaterEqual(record["source_counts"]["approved"], 1)
        self.assertGreaterEqual(record["source_counts"]["enabled"], 1)

    def test_seed_is_idempotent(self):
        self._seed()
        self._seed("--password", "second-pass")
        self.assertEqual(Score.objects.count(), 2)
        self.assertEqual(CompanyProfileObservation.objects.count(), 3)
        self.assertEqual(JobSourceCatalog.objects.count(), 1)
        self.assertEqual(
            JobListing.all_objects.count(), 2
        )
        self.assertEqual(JobMatch.objects.count(), 2)
        user = get_user_model().objects.get(username="staging_user")
        self.assertTrue(user.check_password("second-pass"))

    @override_settings(ENV="prod")
    def test_refuses_to_run_in_production(self):
        with self.assertRaises(CommandError):
            call_command("seed_staging_baseline", stdout=io.StringIO())
        self.assertEqual(Score.objects.count(), 0)
        self.assertFalse(get_user_model().objects.filter(username="staging_user").exists())
