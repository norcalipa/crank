# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the readiness record's ``release_verdict`` and ``data_counts`` (issue #492).

``release_verdict`` is a pure function over the baseline record: one test per
blocker code, the all-clear case, and the real command output. The record
carries the effective ``CapabilitySwitch`` state, so a capability whose
settings flags are on but whose switch is off is never production ready.
``data_counts``
must be aggregate integers only, so two records can be compared across a
rollback without exposing identifiers or text.
"""

import copy
import io
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from crank.management.commands.readiness_baseline import (
    VERDICT_CAPABILITIES,
    baseline_record,
    capability_switches,
    data_counts,
    listing_counts,
    release_verdict,
)
from crank.management.commands.seed_staging_baseline import TARGET_ORG_NAME
from crank.models.company_profile import CompanyFieldEvidence
from crank.models.job_match import JobMatch
from crank.models.job_search import JobSearchConversation, JobSearchMessage
from crank.models.monitoring import ALLOWED_CAPABILITY_KEYS, CapabilitySwitch
from crank.models.organization import Organization
from crank.models.preference import UserPreference

BLOCKER_CODES = (
    "env_not_prod",
    "fixtures_present",
    "provider_not_orchestrator",
    "migrations_not_clean",
    "interactive_agent_disabled",
    "interactive_agent_misconfigured",
    "interactive_agent_switch_disabled",
    "interactive_agent_switch_unknown",
    "job_pipeline_disabled",
    "job_pipeline_misconfigured",
    "job_pipeline_switch_disabled",
    "job_pipeline_switch_unknown",
    "no_enabled_source",
    "inventory_violations",
    "interactive_provider_unbuildable",
    "no_live_listing",
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
        "capability_switches": {"interactive_agent": True, "job_pipeline": True},
        "source_counts": {"configured": 2, "approved": 1, "enabled": 1},
        "assistant_provider": {"builds": True, "offline_placeholder": False},
        "inventory": {"violations": [], "healthy": True},
        "listing_counts": {"active_from_live_sources": 1},
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

    def test_issues_alone_mean_misconfigured(self):
        """A report entry that lists issues but carries no ``ok`` key blocks."""
        for name in VERDICT_CAPABILITIES:
            with self.subTest(name=name):

                def mutate(record, name=name):
                    entry = _capability(record, name)
                    del entry["ok"]
                    entry["issues"] = ["AGENT_RUN_ENABLED is false"]

                self.assertEqual(self._blockers(mutate), [f"{name}_misconfigured"])

    def test_ok_false_alone_means_misconfigured(self):
        for name in VERDICT_CAPABILITIES:
            with self.subTest(name=name):
                self.assertEqual(
                    self._blockers(
                        lambda r, name=name: _capability(r, name).update(
                            ok=False, issues=[]
                        )
                    ),
                    [f"{name}_misconfigured"],
                )

    def test_enabled_entry_without_ok_or_issues_is_not_misconfigured(self):
        record = _ready_record()
        for name in VERDICT_CAPABILITIES:
            entry = _capability(record, name)
            del entry["ok"]
            del entry["issues"]
        self.assertEqual(release_verdict(record)["blockers"], [])

    def test_switch_disabled_blocks_a_settings_enabled_capability(self):
        """Settings flags on, database switch off: not production ready."""
        for name in VERDICT_CAPABILITIES:
            with self.subTest(name=name):
                self.assertEqual(
                    self._blockers(
                        lambda r, name=name: r["capability_switches"].update(
                            {name: False}
                        )
                    ),
                    [f"{name}_switch_disabled"],
                )

    def test_switch_state_must_be_recorded_as_true(self):
        """A record that does not say the switch is on fails closed.

        ``False`` is a switch somebody turned off. Anything else was never
        read: a failed read (``"unknown"``), or a record written before the
        key existed.
        """
        both = [f"{name}_switch_unknown" for name in VERDICT_CAPABILITIES]
        self.assertEqual(self._blockers(lambda r: r.pop("capability_switches")), both)
        self.assertEqual(
            self._blockers(lambda r: r.update(capability_switches=None)), both
        )
        self.assertEqual(
            self._blockers(lambda r: r.update(capability_switches={})), both
        )
        for unread in ("unknown", None, "true", 1, 0, ""):
            with self.subTest(unread=unread):
                self.assertEqual(
                    self._blockers(
                        lambda r, unread=unread: r["capability_switches"].update(
                            job_pipeline=unread
                        )
                    ),
                    ["job_pipeline_switch_unknown"],
                )

    def test_settings_and_switch_blockers_are_independent(self):
        def mutate(record):
            _capability(record, "job_pipeline").update(enabled=False)
            record["capability_switches"]["job_pipeline"] = False

        self.assertEqual(
            self._blockers(mutate),
            ["job_pipeline_disabled", "job_pipeline_switch_disabled"],
        )

    def test_verdict_capabilities_are_registered_switch_keys(self):
        self.assertEqual(VERDICT_CAPABILITIES, ("interactive_agent", "job_pipeline"))
        self.assertTrue(set(VERDICT_CAPABILITIES) <= set(ALLOWED_CAPABILITY_KEYS))

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

    def test_no_live_listing(self):
        cases = {
            "zero": lambda r: r["listing_counts"].update(active_from_live_sources=0),
            "key missing": lambda r: r["listing_counts"].clear(),
            "section missing": lambda r: r.pop("listing_counts"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.assertEqual(self._blockers(mutate), ["no_live_listing"])

    def test_no_live_listing_is_reported_beside_inventory_violations(self):
        """The two codes are independent: neither hides the other."""

        def mutate(record):
            record["inventory"]["violations"] = ["zero active listings"]
            record["listing_counts"]["active_from_live_sources"] = 0

        self.assertEqual(
            self._blockers(mutate), ["inventory_violations", "no_live_listing"]
        )

    def test_provider_that_cannot_be_built_blocks(self):
        """A provider the endpoint cannot build, or the offline placeholder."""
        cases = {
            "does not build": {"builds": False, "offline_placeholder": False},
            "placeholder": {"builds": True, "offline_placeholder": True},
            "build not recorded as true": {"builds": 1, "offline_placeholder": False},
            "placeholder not recorded as false": {"builds": True},
            "section absent": None,
        }
        for label, section in cases.items():
            with self.subTest(label):
                def mutate(record, section=section):
                    if section is None:
                        del record["assistant_provider"]
                    else:
                        record["assistant_provider"] = section

                self.assertEqual(
                    self._blockers(mutate), ["interactive_provider_unbuildable"]
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
                "interactive_agent_switch_unknown",
                "job_pipeline_disabled",
                "job_pipeline_switch_unknown",
                "no_enabled_source",
                "interactive_provider_unbuildable",
                "no_live_listing",
                "no_successful_pipeline_run",
            ],
        )

    def test_every_documented_code_is_reachable(self):
        """The documented codes are exactly the codes the verdict can emit."""
        from pathlib import Path
        import re

        doc = (
            Path(__file__).resolve().parents[3]
            / "docs"
            / "deployment-baseline-2026-09.md"
        ).read_text(encoding="utf-8")
        row = next(
            line for line in doc.splitlines() if line.startswith("| `release_verdict` |")
        )
        codes = row.split("Codes:", 1)[1].split(".", 1)[0]
        self.assertEqual(re.findall(r"`([a-z_]+)`", codes), list(BLOCKER_CODES))

        # Every documented code is produced by some record, and no other is.
        reachable = set(release_verdict({})["blockers"])

        def worst(record):
            record["fixtures"]["present"] = True
            record["inventory"]["violations"] = ["zero active listings"]
            record["listing_counts"]["active_from_live_sources"] = 0
            record["assistant_provider"]["builds"] = False
            for name in VERDICT_CAPABILITIES:
                _capability(record, name)["ok"] = False
                record["capability_switches"][name] = False

        reachable |= set(self._blockers(worst))
        self.assertEqual(reachable, set(BLOCKER_CODES))
        self.assertIn("`release_verdict`", doc)
        self.assertIn("`data_counts`", doc)
        self.assertIn("`capability_switches`", doc)
        self.assertIn("`listing_counts`", doc)


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

    def test_record_carries_the_effective_switch_state(self):
        """Absent rows read as enabled, exactly as the runtime reads them."""
        self.assertEqual(
            capability_switches(), {"interactive_agent": True, "job_pipeline": True}
        )
        CapabilitySwitch.objects.create(key="job_pipeline", enabled=False)
        CapabilitySwitch.objects.create(key="interactive_agent", enabled=True)
        record = self._command_record()
        self.assertEqual(
            record["capability_switches"],
            {"interactive_agent": True, "job_pipeline": False},
        )
        blockers = record["release_verdict"]["blockers"]
        self.assertIn("job_pipeline_switch_disabled", blockers)
        self.assertNotIn("interactive_agent_switch_disabled", blockers)

    @override_settings(AGENT_RUN_ENABLED=True, JOB_PIPELINE_ENABLED=True)
    def test_settings_on_and_switch_off_is_not_production_ready(self):
        """The rollback switch is visible in the verdict, not only the flags."""
        enabled = baseline_record()
        self.assertIs(_capability(enabled, "job_pipeline")["enabled"], True)
        self.assertNotIn(
            "job_pipeline_switch_disabled", enabled["release_verdict"]["blockers"]
        )

        CapabilitySwitch.objects.create(key="job_pipeline", enabled=False)
        record = baseline_record()
        self.assertIs(_capability(record, "job_pipeline")["enabled"], True)
        blockers = record["release_verdict"]["blockers"]
        self.assertIn("job_pipeline_switch_disabled", blockers)
        self.assertNotIn("job_pipeline_disabled", blockers)
        self.assertIs(record["release_verdict"]["production_ready"], False)
        self.assertEqual(record["release_verdict"], release_verdict(record))

    def test_unreadable_switch_is_recorded_as_unknown_and_blocks(self):
        """A failed switch read must never be recorded as enabled."""
        with mock.patch(
            "crank.management.commands.readiness_baseline.CapabilitySwitch.objects"
        ) as manager:
            manager.filter.side_effect = OperationalError("connection lost")
            self.assertEqual(
                capability_switches(),
                {"interactive_agent": "unknown", "job_pipeline": "unknown"},
            )
            record = self._command_record()
        self.assertEqual(
            record["capability_switches"],
            {"interactive_agent": "unknown", "job_pipeline": "unknown"},
        )
        blockers = record["release_verdict"]["blockers"]
        for name in VERDICT_CAPABILITIES:
            self.assertIn(f"{name}_switch_unknown", blockers)
            self.assertNotIn(f"{name}_switch_disabled", blockers)

        # Everything else ready, switches unreadable: still not ready.
        ready = _ready_record()
        ready["capability_switches"] = {
            name: "unknown" for name in VERDICT_CAPABILITIES
        }
        self.assertEqual(
            release_verdict(ready),
            {
                "production_ready": False,
                "blockers": [
                    "interactive_agent_switch_unknown",
                    "job_pipeline_switch_unknown",
                ],
            },
        )

    def test_switch_read_ignores_other_keys_and_reads_each_row(self):
        """Each capability reads its own row; unrelated switches do not leak in."""
        CapabilitySwitch.objects.create(key="interactive_agent", enabled=False)
        CapabilitySwitch.objects.create(key="crawl", enabled=False)
        self.assertEqual(
            capability_switches(), {"interactive_agent": False, "job_pipeline": True}
        )
        CapabilitySwitch.objects.filter(key="interactive_agent").update(enabled=True)
        CapabilitySwitch.objects.create(key="job_pipeline", enabled=False)
        self.assertEqual(
            capability_switches(), {"interactive_agent": True, "job_pipeline": False}
        )

    def _ready_real_state(self):
        """Rows and settings that make the real record all-clear."""
        from crank.models.agent_run import AgentRun
        from crank.models.job import JobListing, JobSourceCatalog

        source = JobSourceCatalog.objects.create(
            name="Live Source",
            adapter_key="usajobs",
            base_url="https://data.usajobs.gov/",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
            last_crawl_at=timezone.now(),
        )
        listing = JobListing.all_objects.create(
            source=source,
            external_id="live-1",
            canonical_url="https://jobs.example.test/listings/live-1",
            employer_name="Example Corp",
            title="Software Engineer",
            status=JobListing.Status.ACTIVE,
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.SUCCEEDED,
        )
        return source, listing

    def _ready_settings(self):
        return override_settings(
            ENV="prod",
            JOB_SEARCH_PROVIDER="orchestrator",
            INTERACTIVE_AGENT_ENABLED=True,
            # A value ``_build_provider()`` can build: the product answers
            # ``ready`` for it. A bare ``"openai"`` passes the capability
            # report and cannot be built.
            LLM_PROVIDER="crank.agents.llm:OpenAIChatAdapter",
            LLM_MODEL="model-x",
            LLM_API_KEY="not-a-real-key",
            JOB_PIPELINE_ENABLED=True,
            AGENT_RUN_ENABLED=True,
        )

    def test_real_record_can_be_production_ready(self):
        """The all-clear verdict, on the record the command builds, not a dict."""
        self._ready_real_state()
        with self._ready_settings():
            record = baseline_record()
        self.assertEqual(
            record["release_verdict"], {"production_ready": True, "blockers": []}
        )
        self.assertEqual(record["listing_counts"], {"active_from_live_sources": 1})
        # Integers only: ``.exists()`` would record ``True``, which equals 1.
        self.assertIs(type(record["listing_counts"]["active_from_live_sources"]), int)
        self.assertEqual(
            record["assistant_provider"],
            {"builds": True, "offline_placeholder": False},
        )
        self.assertEqual(self._assistant_state(), "ready")

    def _assistant_state(self) -> str:
        """The product's own answer, with the real provider factory."""
        from crank.views.assistant_status import _classify_authenticated_state

        return _classify_authenticated_state()

    def test_provider_the_endpoint_cannot_build_blocks_the_all_clear(self):
        """The capability report accepts these; the endpoint cannot serve."""
        self._ready_real_state()
        for value in ("openai", "crank.agents.llm:OpenAIProvider", ""):
            with self.subTest(value):
                with self._ready_settings(), override_settings(LLM_PROVIDER=value):
                    record = baseline_record()
                    state = self._assistant_state()
                self.assertEqual(
                    record["assistant_provider"],
                    {"builds": False, "offline_placeholder": False},
                )
                self.assertEqual(
                    record["release_verdict"]["blockers"],
                    (["interactive_agent_misconfigured"] if not value else [])
                    + ["interactive_provider_unbuildable"],
                )
                self.assertNotEqual(state, "ready")

    def test_offline_placeholder_provider_blocks_the_all_clear(self):
        self._ready_real_state()
        with self._ready_settings(), override_settings(
            LLM_PROVIDER="crank.agents.llm:FakeLLMProvider"
        ):
            record = baseline_record()
        self.assertEqual(
            record["assistant_provider"],
            {"builds": True, "offline_placeholder": True},
        )
        self.assertEqual(
            record["release_verdict"]["blockers"], ["interactive_provider_unbuildable"]
        )

    def test_listings_under_a_disabled_or_blocked_source_are_not_live(self):
        """Matching ignores them, so the verdict must not count them."""
        from crank.models.job import JobSourceCatalog

        source, _ = self._ready_real_state()
        # A second approved, enabled source with no listings keeps
        # ``no_enabled_source`` clear, as a successful empty fetch would.
        JobSourceCatalog.objects.create(
            name="Empty Source",
            adapter_key="usajobs",
            base_url="https://data.usajobs.gov/",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
            last_crawl_at=timezone.now(),
        )
        states = {
            "disabled": {"enabled": False},
            "blocked": {
                "approval_state": JobSourceCatalog.ApprovalState.BLOCKED,
                "enabled": True,
            },
            "pending": {
                "approval_state": JobSourceCatalog.ApprovalState.PENDING,
                "enabled": True,
            },
            "pending, disabled": {
                "approval_state": JobSourceCatalog.ApprovalState.PENDING,
                "enabled": False,
            },
        }
        for label, changes in states.items():
            with self.subTest(label):
                JobSourceCatalog.objects.filter(pk=source.pk).update(**changes)
                with self._ready_settings():
                    record = baseline_record()
                # The old evidence: inventory health still sees a listing.
                self.assertEqual(record["inventory"]["violations"], [])
                self.assertEqual(record["listing_counts"], {"active_from_live_sources": 0})
                self.assertIs(type(record["listing_counts"]["active_from_live_sources"]), int)
                self.assertEqual(
                    record["release_verdict"],
                    {"production_ready": False, "blockers": ["no_live_listing"]},
                )

    def test_inactive_listing_is_not_live(self):
        from crank.models.job import JobListing

        _, listing = self._ready_real_state()
        JobListing.all_objects.filter(pk=listing.pk).update(
            status=JobListing.Status.EXPIRED
        )
        self.assertEqual(listing_counts(), {"active_from_live_sources": 0})

    def test_fixture_backed_record_is_not_production_ready(self):
        call_command("seed_staging_baseline", stdout=io.StringIO())
        record = baseline_record()
        self.assertIs(record["fixtures"]["present"], True)
        self.assertIn("fixtures_present", record["release_verdict"]["blockers"])
        self.assertIs(record["release_verdict"]["production_ready"], False)
        # A decision record still carries the counts that show data preserved.
        self.assertEqual(set(record["data_counts"]), DATA_COUNT_KEYS)


class DataCountsTests(TestCase):
    """``data_counts`` is aggregate integers only."""

    def test_empty_database_counts_are_zero(self):
        counts = data_counts()
        self.assertEqual(set(counts), DATA_COUNT_KEYS)
        self.assertEqual(set(counts.values()), {0})

    def test_each_counter_reads_its_own_table(self):
        """A different number of rows per kind, so no two counters can swap."""
        call_command("seed_staging_baseline", stdout=io.StringIO())
        JobMatch.objects.update(seen_at=None, dismissed=False)
        before = data_counts()
        self.assertEqual(before["job_matches"], JobMatch.objects.count())
        self.assertGreaterEqual(before["job_matches"], 2)

        users = [
            get_user_model().objects.create_user(
                username=f"release-verdict-user-{index}",
                password="not-a-real-password-492",
            )
            for index in range(4)
        ]
        # 2 conversations, 3 messages, 4 saved preferences.
        conversations = [
            JobSearchConversation.objects.create(owner=user) for user in users[:2]
        ]
        for index in range(3):
            JobSearchMessage.objects.create(
                conversation=conversations[index % 2],
                role=JobSearchMessage.Role.USER,
                content="synthetic turn",
            )
        preferences_before = UserPreference.objects.count()
        for user in users:
            UserPreference.objects.get_or_create(user=user)
        added_preferences = UserPreference.objects.count() - preferences_before
        # 1 match seen, 2 dismissed.
        first, second = JobMatch.objects.order_by("id")[:2]
        JobMatch.objects.filter(pk=first.pk).update(seen_at=timezone.now())
        JobMatch.objects.filter(pk__in=[first.pk, second.pk]).update(dismissed=True)
        # 5 accepted evidence rows and 1 conflicted row.
        organization = Organization.objects.get(name=TARGET_ORG_NAME)
        field_keys = [choice[0] for choice in CompanyFieldEvidence.FieldKey.choices]
        for index in range(6):
            CompanyFieldEvidence.objects.create(
                organization=organization,
                field_key=field_keys[index % len(field_keys)],
                state=(
                    CompanyFieldEvidence.State.ACCEPTED
                    if index < 5
                    else CompanyFieldEvidence.State.CONFLICTED
                ),
                value_text=f"synthetic {index}",
                source_url=f"https://jobs.example.test/about/{index}",
                observed_at=timezone.now(),
                validation_version="v1",
                extractor_version="v1",
            )

        after = data_counts()
        delta = {key: after[key] - before[key] for key in DATA_COUNT_KEYS}
        self.assertEqual(
            delta,
            {
                "conversations": 2,
                "messages": 3,
                "saved_preferences": added_preferences,
                "job_matches": 0,
                "job_matches_seen": 1,
                "job_matches_dismissed": 2,
                # Only accepted rows count; the conflicted row is excluded.
                "accepted_company_evidence": 5,
            },
        )
        self.assertEqual(added_preferences, 4)
        # Each key against its own queryset.
        self.assertEqual(
            after,
            {
                "conversations": JobSearchConversation.objects.count(),
                "messages": JobSearchMessage.objects.count(),
                "saved_preferences": UserPreference.objects.count(),
                "job_matches": JobMatch.objects.count(),
                "job_matches_seen": JobMatch.objects.filter(
                    seen_at__isnull=False
                ).count(),
                "job_matches_dismissed": JobMatch.objects.filter(
                    dismissed=True
                ).count(),
                "accepted_company_evidence": CompanyFieldEvidence.objects.filter(
                    state=CompanyFieldEvidence.State.ACCEPTED
                ).count(),
            },
        )
        self.assertNotEqual(after["conversations"], after["messages"])

    def test_record_carries_counts_only(self):
        """No identifier or text: every value is a plain non-negative integer."""
        counts = baseline_record()["data_counts"]
        self.assertEqual(set(counts), DATA_COUNT_KEYS)
        for key, value in counts.items():
            self.assertIs(type(value), int, key)
            self.assertGreaterEqual(value, 0, key)
