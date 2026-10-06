# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the readiness record's ``release_verdict`` and ``data_counts`` (issue #492).

``release_verdict`` is a pure function over the baseline record: one test per
blocker code, the all-clear case, and the real command output. ``data_counts``
must be aggregate integers only, so two records can be compared across a
rollback without exposing identifiers or text.
"""

import copy
import io
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from crank.management.commands.readiness_baseline import (
    baseline_record,
    data_counts,
    release_verdict,
)
from crank.management.commands.seed_staging_baseline import TARGET_ORG_NAME
from crank.models.company_profile import CompanyFieldEvidence
from crank.models.job_match import JobMatch
from crank.models.job_search import JobSearchConversation, JobSearchMessage
from crank.models.organization import Organization
from crank.models.preference import UserPreference

BLOCKER_CODES = (
    "env_not_prod",
    "fixtures_present",
    "provider_not_orchestrator",
    "migrations_not_clean",
    "interactive_agent_disabled",
    "interactive_agent_misconfigured",
    "job_pipeline_disabled",
    "job_pipeline_misconfigured",
    "no_enabled_source",
    "inventory_violations",
    "no_successful_pipeline_run",
)

DATA_COUNT_KEYS = {
    "conversations",
    "messages",
    "saved_preferences",
    "job_matches",
    "job_matches_seen",
    "job_matches_dismissed",
    "accepted_company_evidence",
}


def _ready_record() -> dict:
    """A synthetic record that satisfies every production-readiness condition."""
    return {
        "env": "prod",
        "fixtures": {"present": False, "revision": None},
        "job_search_provider": "orchestrator",
        "migrations": {"applied_count": 48, "pending_count": 0, "status": "clean"},
        "capabilities": {
            "capabilities": [
                {"name": "interactive_agent", "enabled": True, "ok": True, "issues": []},
                {"name": "job_pipeline", "enabled": True, "ok": True, "issues": []},
                {"name": "crawl", "enabled": False, "ok": True, "issues": []},
            ],
        },
        "source_counts": {"configured": 2, "approved": 1, "enabled": 1},
        "inventory": {"violations": [], "healthy": True},
        "latest_runs": [
            {"run_type": "noop", "status": "failed"},
            {"run_type": "job_pipeline", "status": "succeeded"},
        ],
    }


def _capability(record: dict, name: str) -> dict:
    return next(
        cap for cap in record["capabilities"]["capabilities"] if cap["name"] == name
    )


class ReleaseVerdictTests(SimpleTestCase):
    """Each failed condition adds exactly one fixed code."""

    def _blockers(self, mutate) -> list:
        record = _ready_record()
        mutate(record)
        verdict = release_verdict(record)
        self.assertIs(verdict["production_ready"], False)
        return verdict["blockers"]

    def test_all_conditions_met_is_production_ready(self):
        self.assertEqual(
            release_verdict(_ready_record()),
            {"production_ready": True, "blockers": []},
        )

    def test_verdict_does_not_mutate_the_record(self):
        record = _ready_record()
        before = copy.deepcopy(record)
        release_verdict(record)
        self.assertEqual(record, before)

    def test_env_not_prod(self):
        for env in ("staging", "dev", "unknown"):
            with self.subTest(env=env):
                self.assertEqual(
                    self._blockers(lambda r, env=env: r.update(env=env)),
                    ["env_not_prod"],
                )

    def test_fixtures_present(self):
        self.assertEqual(
            self._blockers(
                lambda r: r.update(
                    fixtures={"present": True, "revision": "staging-baseline-1"}
                )
            ),
            ["fixtures_present"],
        )

    def test_provider_not_orchestrator(self):
        self.assertEqual(
            self._blockers(lambda r: r.update(job_search_provider="demo")),
            ["provider_not_orchestrator"],
        )

    def test_migrations_not_clean(self):
        for status in ("pending", "error"):
            with self.subTest(status=status):
                self.assertEqual(
                    self._blockers(
                        lambda r, status=status: r["migrations"].update(status=status)
                    ),
                    ["migrations_not_clean"],
                )

    def test_interactive_agent_disabled(self):
        self.assertEqual(
            self._blockers(
                lambda r: _capability(r, "interactive_agent").update(enabled=False)
            ),
            ["interactive_agent_disabled"],
        )

    def test_interactive_agent_misconfigured(self):
        self.assertEqual(
            self._blockers(
                lambda r: _capability(r, "interactive_agent").update(
                    ok=False, issues=["LLM_API_KEY is missing"]
                )
            ),
            ["interactive_agent_misconfigured"],
        )

    def test_job_pipeline_disabled(self):
        self.assertEqual(
            self._blockers(
                lambda r: _capability(r, "job_pipeline").update(enabled=False)
            ),
            ["job_pipeline_disabled"],
        )

    def test_job_pipeline_misconfigured(self):
        self.assertEqual(
            self._blockers(lambda r: _capability(r, "job_pipeline").update(ok=False)),
            ["job_pipeline_misconfigured"],
        )

    def test_capability_missing_from_report_counts_as_disabled(self):
        self.assertEqual(
            self._blockers(lambda r: r.update(capabilities={"capabilities": []})),
            ["interactive_agent_disabled", "job_pipeline_disabled"],
        )

    def test_no_enabled_source(self):
        self.assertEqual(
            self._blockers(lambda r: r["source_counts"].update(enabled=0)),
            ["no_enabled_source"],
        )

    def test_inventory_violations(self):
        self.assertEqual(
            self._blockers(
                lambda r: r["inventory"].update(violations=["zero active listings"])
            ),
            ["inventory_violations"],
        )

    def test_no_successful_pipeline_run(self):
        cases = {
            "latest run failed": [{"run_type": "job_pipeline", "status": "failed"}],
            "latest run still running": [
                {"run_type": "job_pipeline", "status": "running"}
            ],
            "only another run type succeeded": [
                {"run_type": "noop", "status": "succeeded"}
            ],
            "no runs": [],
        }
        for label, runs in cases.items():
            with self.subTest(label):
                self.assertEqual(
                    self._blockers(lambda r, runs=runs: r.update(latest_runs=runs)),
                    ["no_successful_pipeline_run"],
                )

    def test_empty_record_reports_every_unmet_condition(self):
        """A record with nothing in it fails closed on every readable condition."""
        verdict = release_verdict({})
        self.assertIs(verdict["production_ready"], False)
        self.assertEqual(
            verdict["blockers"],
            [
                "env_not_prod",
                "provider_not_orchestrator",
                "migrations_not_clean",
                "interactive_agent_disabled",
                "job_pipeline_disabled",
                "no_enabled_source",
                "no_successful_pipeline_run",
            ],
        )

    def test_every_documented_code_is_reachable(self):
        """The codes above are exactly the documented set, in the baseline doc too."""
        from pathlib import Path

        doc = (
            Path(__file__).resolve().parents[3]
            / "docs"
            / "deployment-baseline-2026-09.md"
        ).read_text(encoding="utf-8")
        for code in BLOCKER_CODES:
            self.assertIn(f"`{code}`", doc)
        self.assertIn("`release_verdict`", doc)
        self.assertIn("`data_counts`", doc)


class ReleaseVerdictCommandTests(TestCase):
    """The real record carries a verdict consistent with itself."""

    def _command_record(self) -> dict:
        stdout = io.StringIO()
        call_command("readiness_baseline", stdout=stdout, stderr=io.StringIO())
        return json.loads(stdout.getvalue())

    def test_command_output_is_not_production_ready_in_tests(self):
        record = self._command_record()
        verdict = record["release_verdict"]
        self.assertIs(verdict["production_ready"], False)
        self.assertIn("env_not_prod", verdict["blockers"])
        self.assertTrue(set(verdict["blockers"]) <= set(BLOCKER_CODES))
        self.assertEqual(verdict, release_verdict(record))

    def test_fixture_backed_record_is_not_production_ready(self):
        call_command("seed_staging_baseline", stdout=io.StringIO())
        record = baseline_record()
        self.assertIs(record["fixtures"]["present"], True)
        self.assertIn("fixtures_present", record["release_verdict"]["blockers"])
        self.assertIs(record["release_verdict"]["production_ready"], False)


class DataCountsTests(TestCase):
    """``data_counts`` is aggregate integers only."""

    def test_empty_database_counts_are_zero(self):
        counts = data_counts()
        self.assertEqual(set(counts), DATA_COUNT_KEYS)
        self.assertEqual(set(counts.values()), {0})

    def test_counts_one_row_of_each_kind(self):
        call_command("seed_staging_baseline", stdout=io.StringIO())
        before = data_counts()
        self.assertEqual(before["job_matches"], JobMatch.objects.count())
        self.assertGreaterEqual(before["job_matches"], 2)

        user = get_user_model().objects.create_user(
            username="release-verdict-user", password="not-a-real-password-492"
        )
        conversation = JobSearchConversation.objects.create(owner=user)
        JobSearchMessage.objects.create(
            conversation=conversation,
            role=JobSearchMessage.Role.USER,
            content="synthetic turn",
        )
        UserPreference.objects.get_or_create(user=user)
        seen, dismissed = JobMatch.objects.order_by("id")[:2]
        JobMatch.objects.filter(pk=seen.pk).update(seen_at=timezone.now())
        JobMatch.objects.filter(pk=dismissed.pk).update(dismissed=True)
        organization = Organization.objects.get(name=TARGET_ORG_NAME)
        evidence = {
            "organization": organization,
            "value_text": "synthetic",
            "source_url": "https://jobs.example.test/about",
            "observed_at": timezone.now(),
            "validation_version": "v1",
            "extractor_version": "v1",
        }
        CompanyFieldEvidence.objects.create(
            field_key=CompanyFieldEvidence.FieldKey.RTO_POLICY,
            state=CompanyFieldEvidence.State.ACCEPTED,
            **evidence,
        )
        CompanyFieldEvidence.objects.create(
            field_key=CompanyFieldEvidence.FieldKey.FUNDING_ROUND,
            state=CompanyFieldEvidence.State.CONFLICTED,
            **evidence,
        )

        after = data_counts()
        self.assertEqual(after["conversations"], before["conversations"] + 1)
        self.assertEqual(after["messages"], before["messages"] + 1)
        self.assertEqual(
            after["saved_preferences"], UserPreference.objects.count()
        )
        self.assertGreaterEqual(after["saved_preferences"], 1)
        self.assertEqual(after["job_matches"], before["job_matches"])
        self.assertEqual(after["job_matches_seen"], before["job_matches_seen"] + 1)
        self.assertEqual(
            after["job_matches_dismissed"], before["job_matches_dismissed"] + 1
        )
        # Only accepted rows count; the conflicted row is excluded.
        self.assertEqual(
            after["accepted_company_evidence"],
            before["accepted_company_evidence"] + 1,
        )

    def test_record_carries_counts_only(self):
        """No identifier or text: every value is a plain non-negative integer."""
        counts = baseline_record()["data_counts"]
        self.assertEqual(set(counts), DATA_COUNT_KEYS)
        for key, value in counts.items():
            self.assertIs(type(value), int, key)
            self.assertGreaterEqual(value, 0, key)
