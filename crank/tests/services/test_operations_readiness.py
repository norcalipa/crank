# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the read-only operations readiness snapshot (issue #481)."""

import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from crank.agents.jobs.registry import REGISTRY
from crank.models.agent_run import AgentRun
from crank.models.company_profile import CompanyProfileObservation
from crank.models.crawl_run import CrawlRun
from crank.models.employer import UnresolvedEmployer
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.monitoring import CapabilitySwitch
from crank.models.organization import Organization
from crank.models.preference import UserPreference
from crank.models.publication import PublicationEvent
from crank.services import operations_readiness as ops

APPROVED = JobSourceCatalog.ApprovalState.APPROVED
ALL_ON = dict(
    AGENT_RUN_ENABLED=True,
    JOB_PIPELINE_ENABLED=True,
    USAJOBS_AUTH_KEY="usajobs-test-key",
    USAJOBS_USER_AGENT_EMAIL="ops@example.test",
    FIRECRAWL_API_KEY="fc-test-key",
)


def make_source(name="Src", **kwargs):
    defaults = {
        "adapter_key": "usajobs",
        "base_url": "https://data.usajobs.gov/api/search",
        "approval_state": APPROVED,
        "enabled": True,
    }
    defaults.update(kwargs)
    return JobSourceCatalog.objects.create(name=name, **defaults)


def make_listing(source, n=1, organization=None, status=JobListing.Status.ACTIVE):
    now = timezone.now()
    return JobListing.all_objects.create(
        source=source,
        external_id=f"ext-{source.pk}-{n}",
        canonical_url=f"https://data.usajobs.gov/job/{source.pk}-{n}",
        employer_name="Acme",
        title="Engineer",
        first_seen_at=now,
        last_seen_at=now,
        status=status,
        organization=organization,
    )


def make_run(status, *, age_minutes=5, counts=None, error="", finished=True):
    now = timezone.now()
    created = now - timedelta(minutes=age_minutes)
    run = AgentRun.objects.create(
        run_type=AgentRun.RunType.JOB_PIPELINE,
        status=status,
        started_at=created if status != AgentRun.Status.PENDING else None,
        finished_at=(created + timedelta(minutes=2)) if finished else None,
        counts=counts or {},
        error_summary=error,
    )
    AgentRun.objects.filter(pk=run.pk).update(created=created)
    return run


def stage(result, key):
    return next(s for s in result["stages"] if s["key"] == key)


class RequiredSettingsContractTests(SimpleTestCase):
    def test_every_registered_adapter_declares_required_settings(self):
        keys = REGISTRY.keys()
        self.assertIn("usajobs", keys)
        self.assertIn("firecrawl-careers", keys)
        for key in keys:
            with self.subTest(adapter=key):
                self.assertIsInstance(REGISTRY.get(key).required_settings, tuple)
        self.assertEqual(
            REGISTRY.get("usajobs").required_settings,
            ("USAJOBS_AUTH_KEY", "USAJOBS_USER_AGENT_EMAIL"),
        )
        self.assertEqual(REGISTRY.get("firecrawl-careers").required_settings, ("FIRECRAWL_API_KEY",))

    def test_adapters_refuse_to_start_without_declared_settings(self):
        from crank.agents.sources.errors import UnauthorizedSourceError

        class Source:
            base_url = "https://data.usajobs.gov/api/search"
            adapter_key = "usajobs"
            name = "s"
            catalog_metadata = {}
            approval_state = "approved"
            enabled = True

        with override_settings(USAJOBS_AUTH_KEY="", USAJOBS_USER_AGENT_EMAIL=""), patch.dict(
            os.environ, {"USAJOBS_AUTH_KEY": "", "USAJOBS_USER_AGENT_EMAIL": ""}
        ):
            with self.assertRaises(UnauthorizedSourceError):
                REGISTRY.get("usajobs")(Source())

    def test_setting_present_reports_booleans_only(self):
        with override_settings(USAJOBS_AUTH_KEY="secret-value"):
            self.assertIs(ops.setting_present("USAJOBS_AUTH_KEY"), True)
        with override_settings(USAJOBS_AUTH_KEY="   "), patch.dict(os.environ, {"USAJOBS_AUTH_KEY": ""}):
            self.assertIs(ops.setting_present("USAJOBS_AUTH_KEY"), False)
        with override_settings(USAJOBS_AUTH_KEY=""), patch.dict(os.environ, {"USAJOBS_AUTH_KEY": "from-env"}):
            self.assertIs(ops.setting_present("USAJOBS_AUTH_KEY"), True)
        with override_settings(USAJOBS_AUTH_KEY=None), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("USAJOBS_AUTH_KEY", None)
            self.assertIs(ops.setting_present("USAJOBS_AUTH_KEY"), False)

    def test_missing_settings_names_only(self):
        with override_settings(USAJOBS_AUTH_KEY="k", USAJOBS_USER_AGENT_EMAIL=""), patch.dict(
            os.environ, {"USAJOBS_USER_AGENT_EMAIL": ""}
        ):
            self.assertEqual(ops.missing_settings("usajobs"), ["USAJOBS_USER_AGENT_EMAIL"])
        self.assertIsNone(ops.missing_settings("no-such-adapter"))

    def test_stage_keys_are_frozen(self):
        self.assertEqual(
            ops.STAGE_KEYS,
            (
                "source_policy",
                "adapter",
                "credentials",
                "capability",
                "scheduler",
                "consumption",
                "inventory",
                "employers",
                "matches",
            ),
        )

    def test_format_age(self):
        self.assertEqual(ops.format_age(5), "5s")
        self.assertEqual(ops.format_age(125), "2m 5s")
        self.assertEqual(ops.format_age(3 * 3600 + 120), "3h 2m")
        self.assertEqual(ops.format_age(2 * 86400 + 4 * 3600), "2d 4h")
        self.assertEqual(ops.format_age(-4), "0s")


@override_settings(**ALL_ON)
class StageTests(TestCase):
    def test_empty_database_names_source_policy_first(self):
        result = ops.readiness()
        self.assertEqual(stage(result, "source_policy")["status"], ops.UNMET)
        self.assertEqual(result["next_step"]["key"], "source_policy")
        self.assertIn("seed", stage(result, "source_policy")["remediation"].lower())
        self.assertEqual(stage(result, "adapter")["status"], ops.PENDING)
        self.assertEqual(stage(result, "credentials")["status"], ops.PENDING)
        self.assertEqual(stage(result, "inventory")["status"], ops.PENDING)
        self.assertEqual(stage(result, "employers")["status"], ops.PENDING)

    def test_pending_approved_disabled_and_blocked_sources_are_not_live(self):
        make_source("P", approval_state=JobSourceCatalog.ApprovalState.PENDING)
        make_source("D", enabled=False)
        make_source("B", approval_state=JobSourceCatalog.ApprovalState.BLOCKED, enabled=True)
        result = ops.readiness()
        policy = stage(result, "source_policy")
        self.assertEqual(policy["status"], ops.UNMET)
        self.assertIn("3 source(s) exist", policy["remediation"])
        self.assertTrue(policy["admin_url"].endswith("/jobsourcecatalog/"))

    def test_source_policy_met_with_live_source(self):
        make_source()
        self.assertEqual(stage(ops.readiness(), "source_policy")["status"], ops.MET)

    def test_unregistered_adapter_is_unmet(self):
        make_source(adapter_key="ghost-adapter")
        result = ops.readiness()
        adapter = stage(result, "adapter")
        self.assertEqual(adapter["status"], ops.UNMET)
        self.assertIn("no registered adapter", adapter["summary"])
        self.assertIn("usajobs", adapter["remediation"])
        self.assertEqual(result["next_step"]["key"], "adapter")

    def test_off_allowlist_url_is_unmet(self):
        source = make_source()
        JobSourceCatalog.objects.filter(pk=source.pk).update(base_url="https://evil.example.org/x")
        adapter = stage(ops.readiness(), "adapter")
        self.assertEqual(adapter["status"], ops.UNMET)
        self.assertIn("not allowlisted", adapter["summary"])

    def test_adapter_met_when_registered_and_allowlisted(self):
        make_source()
        self.assertEqual(stage(ops.readiness(), "adapter")["status"], ops.MET)

    def test_many_bad_sources_are_bounded_in_text(self):
        for i in range(9):
            make_source(f"ghost-{i}", adapter_key="ghost")
        summary = stage(ops.readiness(), "adapter")["summary"]
        self.assertIn("(+4 more)", summary)

    def test_missing_credentials_names_settings_never_values(self):
        make_source()
        with override_settings(USAJOBS_AUTH_KEY="", USAJOBS_USER_AGENT_EMAIL="ops@example.test"), patch.dict(
            os.environ, {"USAJOBS_AUTH_KEY": ""}
        ):
            credentials = stage(ops.readiness(), "credentials")
        self.assertEqual(credentials["status"], ops.UNMET)
        self.assertIn("USAJOBS_AUTH_KEY", credentials["summary"])
        self.assertNotIn("ops@example.test", credentials["summary"] + credentials["remediation"])

    def test_credentials_are_per_source_not_global(self):
        make_source("usa")
        make_source("fc", adapter_key="firecrawl-careers", base_url="https://remoteok.com/jobs")
        with override_settings(FIRECRAWL_API_KEY=""), patch.dict(os.environ, {"FIRECRAWL_API_KEY": ""}):
            credentials = stage(ops.readiness(), "credentials")
        self.assertEqual(credentials["status"], ops.UNMET)
        self.assertIn("fc: FIRECRAWL_API_KEY", credentials["summary"])
        self.assertNotIn("usa:", credentials["summary"])

    def test_credentials_met(self):
        make_source()
        self.assertEqual(stage(ops.readiness(), "credentials")["status"], ops.MET)

    def test_credentials_wait_when_adapter_unregistered(self):
        make_source(adapter_key="ghost")
        self.assertEqual(stage(ops.readiness(), "credentials")["status"], ops.MET)

    def test_capability_parts_each_block(self):
        cases = [
            ({"AGENT_RUN_ENABLED": False}, "AGENT_RUN_ENABLED is off"),
            ({"JOB_PIPELINE_ENABLED": False}, "JOB_PIPELINE_ENABLED is off"),
        ]
        for overrides, text in cases:
            with self.subTest(text=text), override_settings(**overrides):
                capability = stage(ops.readiness(), "capability")
                self.assertEqual(capability["status"], ops.UNMET)
                self.assertIn(text, capability["summary"])
        CapabilitySwitch.objects.create(key="job_pipeline", enabled=False)
        capability = stage(ops.readiness(), "capability")
        self.assertEqual(capability["status"], ops.UNMET)
        self.assertIn("switch is disabled", capability["summary"])
        self.assertTrue(capability["admin_url"].endswith("/capabilityswitch/"))

    def test_capability_met_absent_switch_counts_enabled(self):
        self.assertEqual(stage(ops.readiness(), "capability")["status"], ops.MET)
        self.assertTrue(ops.pipeline_gate_enabled())

    def test_capability_report_issue_is_surfaced(self):
        class Status:
            name = "job_pipeline"
            issues = ["token=abc123 not ready"]

        class Report:
            capabilities = [Status()]

        with patch("crank.capability.capability_report", return_value=Report()):
            capability = stage(ops.readiness(), "capability")
        self.assertEqual(capability["status"], ops.UNMET)
        self.assertNotIn("abc123", capability["summary"])

    def test_scheduler_unmet_without_recent_finished_run_and_met_with_one(self):
        result = ops.readiness()
        scheduler = stage(result, "scheduler")
        self.assertEqual(scheduler["status"], ops.UNMET)
        self.assertIn("CronJob may be suspended", scheduler["summary"])
        self.assertIn("cannot read CronJob state", scheduler["summary"])
        make_run(AgentRun.Status.SUCCEEDED, age_minutes=30)
        self.assertEqual(stage(ops.readiness(), "scheduler")["status"], ops.MET)

    def test_scheduler_ignores_old_and_unfinished_runs(self):
        make_run(AgentRun.Status.SUCCEEDED, age_minutes=60 * 48)
        self.assertEqual(stage(ops.readiness(), "scheduler")["status"], ops.UNMET)

    def test_every_stage_field_has_expected_type(self):
        make_source()
        for scenario in ({}, {"AGENT_RUN_ENABLED": False}):
            with override_settings(**scenario):
                for item in ops.readiness()["stages"]:
                    with self.subTest(stage=item["key"], scenario=scenario):
                        self.assertIsInstance(item["remediation"], str)
                        self.assertIsInstance(item["summary"], str)
                        self.assertTrue(item["admin_url"] == "" or item["admin_url"].startswith("/admin/"))

    def test_consumption_states(self):
        first = stage(ops.readiness(), "consumption")
        self.assertEqual(first["status"], ops.PENDING)
        self.assertTrue(first["admin_url"].endswith("run_type__exact=job_pipeline"))
        AgentRun.objects.all().delete()
        make_run(AgentRun.Status.PENDING, age_minutes=2, finished=False)
        queued = stage(ops.readiness(), "consumption")
        self.assertEqual(queued["status"], ops.PENDING)
        self.assertIn("not yet consumed", queued["summary"])
        AgentRun.objects.all().delete()
        make_run(AgentRun.Status.PENDING, age_minutes=600, finished=False)
        expired = stage(ops.readiness(), "consumption")
        self.assertEqual(expired["status"], ops.UNMET)
        self.assertIn("past its TTL", expired["summary"])

    def test_consumption_failed_reclaimed_running_and_succeeded(self):
        for error, expected in [
            ("stale run reclaimed", "reclaimed as stale"),
            ("queued run reclaimed", "expired in the queue"),
            ("boom", "failed"),
        ]:
            AgentRun.objects.all().delete()
            make_run(AgentRun.Status.FAILED, error=error)
            consumption = stage(ops.readiness(), "consumption")
            self.assertEqual(consumption["status"], ops.UNMET)
            self.assertIn(expected, consumption["summary"])
        AgentRun.objects.all().delete()
        make_run(AgentRun.Status.RUNNING, finished=False)
        self.assertEqual(stage(ops.readiness(), "consumption")["status"], ops.MET)
        AgentRun.objects.all().delete()
        make_run(AgentRun.Status.SUCCEEDED)
        self.assertEqual(stage(ops.readiness(), "consumption")["status"], ops.MET)

    def test_inventory_states(self):
        source = make_source()
        no_listings = stage(ops.readiness(), "inventory")
        self.assertEqual(no_listings["status"], ops.UNMET)
        self.assertIn("first crawl batch", no_listings["remediation"])
        make_listing(source)
        stale = stage(ops.readiness(), "inventory")
        self.assertEqual(stale["status"], ops.UNMET)
        self.assertIn("no source has succeeded", stale["summary"])
        JobSourceCatalog.objects.filter(pk=source.pk).update(last_crawl_at=timezone.now())
        self.assertEqual(stage(ops.readiness(), "inventory")["status"], ops.MET)

    def test_inventory_attention_for_partially_stale_sources(self):
        fresh = make_source("fresh", last_crawl_at=timezone.now())
        make_source("old", last_crawl_at=timezone.now() - timedelta(days=9))
        make_listing(fresh)
        inventory = stage(ops.readiness(), "inventory")
        self.assertEqual(inventory["status"], ops.ATTENTION)
        self.assertIn("1 stale", inventory["summary"])

    def test_inventory_attention_reports_repeated_failures_and_collapse(self):
        source = make_source("s", last_crawl_at=timezone.now())
        make_listing(source, status=JobListing.Status.ACTIVE)
        other = make_source("collapsed", adapter_key="firecrawl-careers", base_url="https://remoteok.com/jobs", last_crawl_at=timezone.now())
        base = timezone.now()
        for i in range(3):
            CrawlRun.objects.create(
                source_type=CrawlRun.SourceType.JOB,
                source_key="usajobs",
                job_source=source,
                outcome=CrawlRun.Outcome.FAILURE,
                started_at=base - timedelta(minutes=i),
            )
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            source_key="fc",
            job_source=other,
            outcome=CrawlRun.Outcome.SUCCESS,
            started_at=base,
            counts={"listings_ingested": 4},
        )
        summary = stage(ops.readiness(), "inventory")["summary"]
        self.assertIn("repeatedly failing", summary)
        self.assertIn("collapsed to zero listings", summary)

    def test_employers_states(self):
        source = make_source(last_crawl_at=timezone.now())
        listing = make_listing(source)
        unresolved = stage(ops.readiness(), "employers")
        self.assertEqual(unresolved["status"], ops.UNMET)
        self.assertIn("none resolved", unresolved["summary"])
        org = Organization.objects.create(name="Acme Org")
        JobListing.all_objects.filter(pk=listing.pk).update(organization=org)
        self.assertEqual(stage(ops.readiness(), "employers")["status"], ops.MET)
        UnresolvedEmployer.objects.create(
            listing=listing, employer_name="Other", reason=UnresolvedEmployer.Reason.NO_MATCH
        )
        attention = stage(ops.readiness(), "employers")
        self.assertEqual(attention["status"], ops.ATTENTION)
        self.assertIn("1 unresolved", attention["summary"])

    def test_matches_live_reads_when_gate_off(self):
        self.assertEqual(stage(ops.readiness(), "matches")["status"], ops.MET)
        user = User.objects.create_user("u1", password="pw")
        UserPreference.objects.create(user=user)
        unmet = stage(ops.readiness(), "matches")
        self.assertEqual(unmet["status"], ops.UNMET)
        self.assertIn("Live reads (committed generations not served)", unmet["summary"])
        source = make_source()
        listing = make_listing(source)
        now = timezone.now()
        JobMatch.objects.create(
            user=user,
            listing=listing,
            preference_version=1,
            ranker_version="r1",
            score=0.5,
            first_matched_at=now,
            last_matched_at=now,
        )
        met = stage(ops.readiness(), "matches")
        self.assertEqual(met["status"], ops.MET)
        self.assertIn("matches exist", met["summary"])

    @override_settings(MATCH_RESULTS_READ_ENABLED=True)
    def test_matches_committed_reads_with_dirty_preferences_is_unmet(self):
        user = User.objects.create_user("u2", password="pw")
        UserPreference.objects.create(user=user)
        matches = stage(ops.readiness(), "matches")
        self.assertEqual(matches["status"], ops.UNMET)
        self.assertIn("preference dirty: 1", matches["summary"])

    @override_settings(MATCH_RESULTS_READ_ENABLED=True)
    def test_matches_committed_reads_clean_is_met_and_lag_makes_unmet(self):
        self.assertEqual(stage(ops.readiness(), "matches")["status"], ops.MET)
        user = User.objects.create_user("u3", password="pw")
        pref = UserPreference.objects.create(user=user)
        MatchResultState.objects.create(
            user=user,
            issued_generation=1,
            current_generation=1,
            preference_revision=pref.revision,
            preference_version=pref.schema_version,
            data_revision=0,
            generated_at=timezone.now(),
        )
        with patch.object(ops, "publication_match_lag_seconds", return_value=10**7):
            lagging = stage(ops.readiness(), "matches")
        self.assertEqual(lagging["status"], ops.UNMET)

    def test_stage_failure_is_unknown_and_isolated(self):
        make_source()

        def boom(ctx):
            raise RuntimeError("Authorization: Bearer abc.def.ghi failure")

        with patch.dict(ops._STAGE_FUNCS, {"adapter": boom}), self.assertLogs(ops.logger, "WARNING") as logs:
            result = ops.readiness()
        adapter = stage(result, "adapter")
        self.assertEqual(adapter["status"], ops.UNKNOWN)
        self.assertIn("Could not compute", adapter["summary"])
        self.assertEqual(stage(result, "source_policy")["status"], ops.MET)
        self.assertNotIn("abc.def.ghi", "\n".join(logs.output))

    def test_next_step_ordering(self):
        with patch.dict(
            ops._STAGE_FUNCS,
            {
                key: (lambda status: lambda ctx: ops._result("adapter", status, "x"))(status)
                for key, status in {
                    "source_policy": ops.MET,
                    "adapter": ops.PENDING,
                    "credentials": ops.UNMET,
                    "capability": ops.UNMET,
                    "scheduler": ops.MET,
                    "consumption": ops.MET,
                    "inventory": ops.MET,
                    "employers": ops.MET,
                    "matches": ops.MET,
                }.items()
            },
        ):
            pass
        statuses = {k: ops.MET for k in ops.STAGE_KEYS}

        def run(overrides):
            merged = {**statuses, **overrides}
            funcs = {
                key: (lambda k, st: lambda ctx: ops._result(k, st, "x"))(key, st) for key, st in merged.items()
            }
            with patch.dict(ops._STAGE_FUNCS, funcs):
                return ops.readiness()

        first_unmet = run({"adapter": ops.PENDING, "credentials": ops.UNMET, "capability": ops.UNMET})
        self.assertEqual(first_unmet["next_step"]["key"], "credentials")
        only_pending = run({"consumption": ops.PENDING, "matches": ops.PENDING})
        self.assertEqual(only_pending["next_step"]["key"], "consumption")
        unknown_only = run({"inventory": ops.UNKNOWN, "employers": ops.ATTENTION})
        self.assertEqual(unknown_only["next_step"]["key"], "inventory")
        done = run({"employers": ops.ATTENTION})
        self.assertIsNone(done["next_step"])
        self.assertTrue(done["all_met"])


@override_settings(**ALL_ON)
class BacklogAndProgressTests(TestCase):
    def test_backlog_shape_is_scalar_only(self):
        result = ops.backlog()
        self.assertEqual(result["employers"], {"unresolved": 0})
        self.assertEqual(result["review"], {"observations_pending": 0, "observations_conflicted": 0})
        self.assertEqual(result["outbox"]["pending"], 0)
        self.assertIsNone(result["outbox"]["oldest_pending_at"])
        self.assertIsNone(result["outbox"]["oldest_pending_age_seconds"])
        self.assertIs(result["outbox"]["consumer_enabled"], False)
        self.assertEqual(result["matches"]["lag_seconds"], 0)
        self.assertEqual(result["matches"]["users_without_generation"], 0)

        def scalar(value):
            if isinstance(value, dict):
                return all(scalar(v) for v in value.values())
            return value is None or isinstance(value, (int, str, bool))

        self.assertTrue(scalar(result))

    def test_backlog_counts_review_outbox_and_generations(self):
        now = timezone.now()
        for status in (
            CompanyProfileObservation.Status.PENDING,
            CompanyProfileObservation.Status.PENDING,
            CompanyProfileObservation.Status.CONFLICTED,
            CompanyProfileObservation.Status.ACCEPTED,
        ):
            CompanyProfileObservation.objects.create(
                source_url="https://example.test/x",
                observed_at=now,
                extraction_version="v1",
                status=status,
            )
        event = PublicationEvent.objects.create(
            target_type="organization", target_id=1, event_kind="changed"
        )
        PublicationEvent.objects.filter(pk=event.pk).update(created_at=now - timedelta(minutes=10))
        PublicationEvent.objects.create(
            target_type="organization", target_id=2, event_kind="changed", processed_at=now
        )
        user = User.objects.create_user("bl", password="pw")
        UserPreference.objects.create(user=user)
        with override_settings(PUBLICATION_CONSUMER_ENABLED=True):
            result = ops.backlog(now)
        self.assertEqual(result["review"], {"observations_pending": 2, "observations_conflicted": 1})
        self.assertEqual(result["outbox"]["pending"], 1)
        self.assertEqual(result["outbox"]["oldest_pending_age_seconds"], 600)
        self.assertTrue(result["outbox"]["consumer_enabled"])
        self.assertEqual(result["matches"]["users_without_generation"], 1)

    def test_publication_match_lag(self):
        now = timezone.now()
        self.assertEqual(ops.publication_match_lag_seconds(now), 0)
        user = User.objects.create_user("lag", password="pw")
        state = MatchResultState.objects.create(
            user=user, issued_generation=1, current_generation=1, data_revision=None
        )
        self.assertEqual(ops.publication_match_lag_seconds(now), 0)
        first = PublicationEvent.objects.create(target_type="listing", target_id=1, event_kind="ingested")
        second = PublicationEvent.objects.create(target_type="listing", target_id=2, event_kind="ingested")
        PublicationEvent.objects.filter(pk=first.pk).update(created_at=now - timedelta(seconds=900))
        PublicationEvent.objects.filter(pk=second.pk).update(created_at=now - timedelta(seconds=60))
        self.assertEqual(ops.publication_match_lag_seconds(now), 900)
        MatchResultState.objects.filter(pk=state.pk).update(data_revision=first.pk)
        self.assertEqual(ops.publication_match_lag_seconds(now), 60)
        MatchResultState.objects.filter(pk=state.pk).update(data_revision=second.pk)
        self.assertEqual(ops.publication_match_lag_seconds(now), 0)

    def test_source_rows_cap_order_and_sanitize(self):
        for i in range(53):
            make_source(f"src-{i:02d}", adapter_key="usajobs" if i % 2 else "ghost")
        source = JobSourceCatalog.objects.get(name="src-01")
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            source_key="usajobs",
            job_source=source,
            outcome=CrawlRun.Outcome.FAILURE,
            started_at=timezone.now(),
            error_summary="Authorization: Bearer topsecrettoken " + "y" * 600,
        )
        rows = ops.source_rows()
        self.assertEqual(rows["total"], 53)
        self.assertEqual(rows["shown"], 50)
        self.assertTrue(rows["truncated"])
        self.assertEqual([r["name"] for r in rows["rows"]][:2], ["src-00", "src-01"])
        first = rows["rows"][0]
        self.assertFalse(first["adapter_registered"])
        self.assertIsNone(first["credentials_present"])
        second = rows["rows"][1]
        self.assertEqual(second["latest_outcome"], "failure")
        self.assertLessEqual(len(second["latest_error"]), 200)
        self.assertNotIn("topsecrettoken", second["latest_error"])
        small = ops.source_rows(limit=100)
        self.assertFalse(small["truncated"] and small["shown"] == 53)

    def test_source_row_details(self):
        now = timezone.now()
        make_source(
            "detail",
            last_crawl_at=now - timedelta(hours=3),
            last_attempt_at=now - timedelta(minutes=5),
            consecutive_failures=2,
        )
        with override_settings(USAJOBS_AUTH_KEY=""), patch.dict(os.environ, {"USAJOBS_AUTH_KEY": ""}):
            row = ops.source_rows(now=now)["rows"][0]
        self.assertTrue(row["adapter_registered"])
        self.assertIs(row["credentials_present"], False)
        self.assertEqual(row["last_success_age"], "3h 0m")
        self.assertEqual(row["last_attempt_age"], "5m 0s")
        self.assertEqual(row["consecutive_failures"], 2)
        self.assertEqual(row["latest_outcome"], "")
        self.assertEqual(row["latest_error"], "")

    def test_run_progress_empty(self):
        progress = ops.run_progress()
        self.assertIsNone(progress["latest"])
        self.assertIsNone(progress["completed"])

    def test_run_progress_queued_is_not_completed(self):
        make_run(AgentRun.Status.PENDING, age_minutes=3, finished=False)
        progress = ops.run_progress()
        self.assertTrue(progress["latest"]["queued_only"])
        self.assertIsNone(progress["completed"])
        self.assertEqual(progress["latest"]["started_at"], None)
        self.assertEqual(progress["latest"]["started_age"], "")

    def test_run_progress_completed_counts_and_links(self):
        counts = {
            "sources_total": 2,
            "sources_succeeded": 1,
            "listings_ingested": 7,
            "employers_resolved": 5,
            "matches_persisted": 4,
            "ignored_key": 9,
            "users_total": True,
            "users_failed": "bad",
        }
        run = make_run(AgentRun.Status.FAILED, counts=counts, error="Bearer abc.def " + "z" * 500)
        make_run(AgentRun.Status.PENDING, age_minutes=1, finished=False)
        progress = ops.run_progress()
        self.assertTrue(progress["latest"]["queued_only"])
        completed = progress["completed"]
        self.assertEqual(completed["id"], run.pk)
        self.assertEqual(completed["status"], "failed")
        self.assertEqual(completed["duration"], "2m 0s")
        self.assertLessEqual(len(completed["error_summary"]), 300)
        self.assertNotIn("abc.def", completed["error_summary"])
        flat = {item["key"]: item["value"] for group in completed["stages"] for item in group["counts"]}
        self.assertEqual(
            flat,
            {
                "sources_total": 2,
                "sources_succeeded": 1,
                "listings_ingested": 7,
                "employers_resolved": 5,
                "matches_persisted": 4,
            },
        )
        urls = [link["url"] for link in completed["links"]]
        self.assertTrue(any(f"agent_run__id__exact={run.pk}" in url for url in urls))

    def test_run_progress_tolerates_non_dict_counts(self):
        run = make_run(AgentRun.Status.SUCCEEDED)
        AgentRun.objects.filter(pk=run.pk).update(counts=[1, 2])
        completed = ops.run_progress()["completed"]
        self.assertTrue(all(group["counts"] == [] for group in completed["stages"]))
