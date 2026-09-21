# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the readiness-baseline command and staging fixture loader (issue #455).

Covers the required baseline record shape, the three distinguishable failure
conditions (disabled provider/source, missing schema, failed completed run),
unconditional secret redaction (including short and overlapping values), the
exact migration-leaf and fixture-revision fields, the adapter/credential
readiness gates, ``--out`` file writing, the staging fixture smoke load, the
rankings and provenance consumers the replay depends on, and staff-account
password provisioning.
"""

import io
import json
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from crank.management.commands.readiness_baseline import (
    baseline_record,
    migration_leaves,
)
from crank.models.agent_run import AgentRun
from crank.models.company_profile import CompanyProfileObservation
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.organization import Organization
from crank.models.score import Score, ScoreAlgorithm, ScoreAlgorithmWeight
from crank.settings.base import DEFAULT_ALGORITHM_ID
from crank.views.index import IndexView

SECRET_A = "x" * 40
SECRET_B = "u" * 32
SECRET_C = "f" * 24

_LOC_MEM_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _run(*args):
    stdout = io.StringIO()
    stderr = io.StringIO()
    call_command("readiness_baseline", *args, stdout=stdout, stderr=stderr, no_color=True)
    return stdout.getvalue(), stderr.getvalue()


def _parse(payload: str) -> dict:
    # The command may append a styled notice after the JSON payload; JSON
    # itself is emitted first and is a single pretty-printed document.
    return json.loads(payload)


def _seed_staging(*args):
    stdout = io.StringIO()
    call_command("seed_staging_baseline", *args, stdout=stdout, no_color=True)
    return stdout.getvalue()


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
            "migration_leaves",
            "fixtures",
            "readiness_gates",
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

    @override_settings(
        LLM_API_KEY="abc",
        USAJOBS_AUTH_KEY="def",
        FIRECRAWL_API_KEY="ghi",
        SECRET_KEY="zz",
    )
    def test_short_secret_values_never_appear_in_output(self):
        # AC-6 is unconditional: short staging/test values must be scrubbed
        # exactly like long production ones.
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
            error_summary="connection refused with key abc usa=def fire=ghi and app key zz",
        )
        payload, _ = _run()
        for secret in ("abc", "def", "ghi", "zz"):
            self.assertNotIn(secret, payload)
        record = _parse(payload)
        by_type = {run["run_type"]: run for run in record["latest_runs"]}
        self.assertIn("[redacted]", by_type["job_pipeline"]["error_summary"])

    @override_settings(
        LLM_API_KEY="abcdefg",
        USAJOBS_AUTH_KEY="abc",
        SECRET_KEY="s" * 50,
    )
    def test_overlapping_secret_values_are_fully_scrubbed(self):
        # Longest-first scrubbing: the short secret must be redacted even
        # where it overlaps a longer configured secret's text.
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
            error_summary="auth abcdefg plus short abc and tail abchij",
        )
        payload, _ = _run()
        self.assertNotIn("abcdefg", payload)
        self.assertNotIn("abc", payload)
        record = _parse(payload)
        by_type = {run["run_type"]: run for run in record["latest_runs"]}
        summary = by_type["job_pipeline"]["error_summary"]
        self.assertIn("[redacted]", summary)
        self.assertIn("[redacted]hij", summary)

    def test_migration_leaves_name_exact_deployed_revision(self):
        payload, _ = _run()
        leaves = _parse(payload)["migration_leaves"]
        self.assertEqual(leaves["status"], "ok")
        self.assertEqual(leaves["pending"], [])
        self.assertEqual(leaves["pending_count"], 0)
        self.assertFalse(leaves["truncated"])
        # The exact deployed revision: the crank app's applied leaf migration.
        # The two leaves (0031 from #470, 0033 from #495/#496) were merged
        # by 0034, 0035 (#459) stacks on top of that merged head, and 0036
        # (#460) stacks on top of 0035.
        self.assertIn(
            "crank.0036_company_field_evidence",
            leaves["applied"],
        )
        self.assertEqual(leaves["applied_count"], len(leaves["applied"]))

    def test_migration_leaves_fail_closed_on_db_error(self):
        with mock.patch(
            "crank.management.commands.readiness_baseline.MigrationExecutor",
            side_effect=RuntimeError("db unavailable"),
        ):
            leaves = migration_leaves()
        self.assertEqual(leaves["status"], "error")
        self.assertIsNone(leaves["applied"])
        self.assertIsNone(leaves["pending"])

    def test_migration_leaves_are_bounded(self):
        class FakeGraph:
            @staticmethod
            def leaf_nodes():
                return [("app", f"{i:04d}_migration") for i in range(1, 31)] + [
                    ("app", "0001_initial")
                ]

        class FakeLoader:
            applied_migrations = {("app", "0001_initial")}
            graph = FakeGraph()

        class FakeExecutor:
            loader = FakeLoader()

        with mock.patch(
            "crank.management.commands.readiness_baseline.MigrationExecutor",
            return_value=FakeExecutor(),
        ):
            leaves = migration_leaves()
        self.assertEqual(leaves["status"], "ok")
        self.assertEqual(leaves["applied"], ["app.0001_initial"])
        self.assertEqual(leaves["applied_count"], 1)
        self.assertEqual(leaves["pending_count"], 30)
        self.assertEqual(len(leaves["pending"]), 25)
        self.assertTrue(leaves["truncated"])

    def test_fixture_set_absent_without_fixtures(self):
        payload, _ = _run()
        record = _parse(payload)
        self.assertEqual(record["fixtures"], {"present": False, "revision": None})

    @override_settings(USAJOBS_AUTH_KEY="", FIRECRAWL_API_KEY="")
    def test_readiness_gates_record_missing_credentials(self):
        payload, _ = _run()
        gates = _parse(payload)["readiness_gates"]
        self.assertFalse(gates["credentials_configured"])
        self.assertFalse(gates["usajobs_credentials_configured"])
        self.assertFalse(gates["firecrawl_credentials_configured"])
        for key in (
            "adapter_registered",
            "adapter_count",
            "adapters",
            "usajobs_adapter_registered",
            "firecrawl_adapter_registered",
            "pipeline_enabled",
            "scheduler_enabled",
        ):
            self.assertIn(key, gates)
        self.assertIsInstance(gates["pipeline_enabled"], bool)
        self.assertIsInstance(gates["scheduler_enabled"], bool)

    @override_settings(USAJOBS_AUTH_KEY="u" * 40, FIRECRAWL_API_KEY="")
    def test_readiness_gates_record_present_credential(self):
        payload, _ = _run()
        gates = _parse(payload)["readiness_gates"]
        self.assertTrue(gates["credentials_configured"])
        self.assertTrue(gates["usajobs_credentials_configured"])
        self.assertFalse(gates["firecrawl_credentials_configured"])

    def test_readiness_gates_mirror_adapter_registry(self):
        payload, _ = _run()
        gates = _parse(payload)["readiness_gates"]
        self.assertTrue(gates["adapter_registered"])
        self.assertGreaterEqual(gates["adapter_count"], 2)
        self.assertIn("usajobs", gates["adapters"])
        self.assertIn("firecrawl-careers", gates["adapters"])
        self.assertTrue(gates["usajobs_adapter_registered"])
        self.assertTrue(gates["firecrawl_adapter_registered"])

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
    def test_fixture_smoke_load_and_baseline(self):
        output = _seed_staging()
        self.assertIn("Staging baseline fixtures ready", output)

        # Rankings dependencies: default algorithm + weight for the fixture type.
        algorithm = ScoreAlgorithm.objects.get(id=DEFAULT_ALGORITHM_ID)
        score_type = Score.objects.get(status=1).type
        self.assertEqual(
            ScoreAlgorithmWeight.objects.filter(
                algorithm=algorithm, type=score_type
            ).count(),
            1,
        )

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

        # Company evidence: accepted / stale / conflicted, with the accepted
        # observation strictly the latest (what the ordinary provenance
        # surface returns).
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
        latest = observations.order_by("-observed_at", "-id").first()
        self.assertEqual(latest.status, CompanyProfileObservation.Status.ACCEPTED)

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

        # Accounts: ordinary (throwaway password) + staff (unusable password
        # until explicitly provisioned via --staff-password).
        staff = get_user_model().objects.get(username="staging_staff")
        self.assertFalse(user.is_staff)
        self.assertTrue(staff.is_staff)
        self.assertTrue(user.check_password("staging-baseline-throwaway"))
        self.assertFalse(staff.has_usable_password())

        # The baseline command runs against the loaded fixtures: source counts
        # and the fixture-set revision are recorded, and output stays JSON-safe.
        payload, _ = _run()
        record = _parse(payload)
        self.assertGreaterEqual(record["source_counts"]["approved"], 1)
        self.assertGreaterEqual(record["source_counts"]["enabled"], 1)
        self.assertEqual(
            record["fixtures"],
            {"present": True, "revision": "staging-baseline-1"},
        )

    def test_seed_is_idempotent(self):
        _seed_staging()
        _seed_staging("--password", "second-pass")
        self.assertEqual(Score.objects.count(), 2)
        self.assertEqual(ScoreAlgorithm.objects.count(), 1)
        self.assertEqual(ScoreAlgorithmWeight.objects.count(), 1)
        self.assertEqual(CompanyProfileObservation.objects.count(), 3)
        self.assertEqual(JobSourceCatalog.objects.count(), 1)
        self.assertEqual(
            JobListing.all_objects.count(),
            2
        )
        self.assertEqual(JobMatch.objects.count(), 2)
        user = get_user_model().objects.get(username="staging_user")
        self.assertTrue(user.check_password("second-pass"))

    def test_staff_account_requires_explicit_provisioning(self):
        _seed_staging()
        staff = get_user_model().objects.get(username="staging_staff")
        self.assertTrue(staff.is_staff)
        self.assertFalse(staff.has_usable_password())
        # The repo-documented ordinary throwaway never works on the staff
        # account.
        self.assertFalse(staff.check_password("staging-baseline-throwaway"))

        # Explicit operator provisioning sets the password...
        _seed_staging("--staff-password", "operator-supplied-secret-1")
        staff = get_user_model().objects.get(username="staging_staff")
        self.assertTrue(staff.check_password("operator-supplied-secret-1"))

        # ...and re-running without the flag never resets it.
        _seed_staging()
        staff = get_user_model().objects.get(username="staging_staff")
        self.assertTrue(staff.check_password("operator-supplied-secret-1"))

    @override_settings(ENV="prod")
    def test_refuses_to_run_in_production(self):
        with self.assertRaises(CommandError):
            call_command("seed_staging_baseline", stdout=io.StringIO())
        self.assertEqual(Score.objects.count(), 0)
        self.assertFalse(get_user_model().objects.filter(username="staging_user").exists())


@override_settings(CACHES=_LOC_MEM_CACHES)
class SeedStagingReplayConsumerTests(TestCase):
    """Exercise the actual consumers the documented replay relies on."""

    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def test_rankings_consumer_returns_fixture_organization(self):
        # The rankings replay must work on a fresh database: IndexView needs
        # the default ScoreAlgorithm and its ScoreAlgorithmWeight, which the
        # seed command now creates.
        _seed_staging()
        request = self.factory.get("/")
        SessionMiddleware(lambda request: None).process_request(request)
        request.session.save()
        view = IndexView()
        view.request = request
        rankings = view.get_queryset()
        names = [row["name"] for row in rankings]
        self.assertIn(
            "Staging Example Corp",
            names,
            "rankings consumer should list the fixture organization",
        )
        target = next(
            row for row in rankings if row["name"] == "Staging Example Corp"
        )
        self.assertEqual(target["ranking"], 1)
        self.assertIsNotNone(target["avg_score"])

    def test_ordinary_provenance_surface_returns_accepted_observation(self):
        # The ordinary company-details replay claim: the provenance surface
        # (latest observation only) returns the accepted row, because the
        # conflicted fixture is seeded strictly older.
        _seed_staging()
        user = get_user_model().objects.get(username="staging_user")
        org = Organization.objects.get(name="Staging Example Corp")
        client = Client()
        client.force_login(user)
        response = client.get(reverse("organization-provenance", args=[org.pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        latest = data["latest_observation"]
        self.assertEqual(latest["status"], "accepted")
        self.assertEqual(latest["observed_domain"], "jobs.example.test")
        # The stale and conflicted rows exist as fixture evidence but are not
        # what the ordinary latest-observation surface returns.
        statuses = set(
            CompanyProfileObservation.objects.filter(
                organization=org
            ).values_list("status", flat=True)
        )
        self.assertIn(CompanyProfileObservation.Status.CONFLICTED, statuses)
