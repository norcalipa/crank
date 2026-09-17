# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for freshness-aware crawl scheduling and command guardrails."""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from crank.models.job import JobSourceCatalog
from crank.models.source import ApprovalState, SourceCatalog
from crank.services.crawl_scheduler import (
    PHASE_JOBS,
    PHASE_ORGANIZATIONS,
    is_stale,
    plan_crawls,
)


def make_job(name, **values):
    defaults = {
        "adapter_key": "fixture-adapter",
        "base_url": "https://jobs.example.test",
        "approval_state": JobSourceCatalog.ApprovalState.APPROVED,
        "enabled": True,
    }
    defaults.update(values)
    return JobSourceCatalog.objects.create(name=name, **defaults)


def make_org_source(name, organization, **values):
    defaults = {
        "adapter_key": "fixture-adapter",
        "base_url": "https://www.google.com",
        "approval_state": ApprovalState.APPROVED,
        "enabled": True,
        "organization": organization,
    }
    defaults.update(values)
    return SourceCatalog.objects.create(name=name, **defaults)


class CrawlFreshnessTests(TestCase):
    def test_invalid_phase_is_rejected(self):
        with self.assertRaises(ValueError):
            plan_crawls(phase="unsupported")

    @patch("crank.services.crawl_scheduler.monitoring.record_event")
    def test_jobs_phase_is_a_documented_noop(self, event):
        """Job ingestion has a single owner (run_job_pipeline): the scheduler's
        jobs phase counts sources and dispatches nothing (issue #462)."""
        make_job("Example jobs")
        with patch(
            "crank.services.crawl_scheduler.crawl_company_profile"
        ) as crawl:
            counts = plan_crawls(phase=PHASE_JOBS)
        crawl.assert_not_called()
        self.assertEqual(counts["scheduled"], 0)
        self.assertEqual(counts["stale"], 0)
        self.assertEqual(counts["jobs_total"], 1)
        self.assertEqual(counts["skipped"], 1)
        event.assert_called_once_with("crawl_planning", counts)

    @patch("crank.services.crawl_scheduler.monitoring.record_event")
    def test_all_phase_no_longer_dispatches_job_sources(self, event):
        """PHASE_ALL plans organization profiles only; job sources stay put."""
        from crank.models.organization import Organization

        organization = Organization.objects.create(name="Example", url="https://example.test")
        org_source = make_org_source("Example rating", organization)
        job_source = make_job("Example jobs")
        with patch(
            "crank.services.crawl_scheduler.crawl_company_profile",
            return_value=type("Result", (), {"errors": 0})(),
        ) as crawl:
            counts = plan_crawls(phase="all")
        crawl.assert_called_once()
        self.assertEqual(crawl.call_args.args[0].pk, org_source.pk)
        self.assertIsNotNone(crawl.call_args.kwargs["now"])
        org_source.refresh_from_db()
        self.assertIsNotNone(org_source.last_crawl_at)
        self.assertEqual(counts["organizations_total"], 1)
        self.assertEqual(counts["jobs_total"], 0)
        job_source.refresh_from_db()
        self.assertIsNone(job_source.last_crawl_at)

    def test_default_organization_dispatcher_is_used(self):
        from crank.models.organization import Organization

        organization = Organization.objects.create(name="Example", url="https://example.test")
        org_source = make_org_source("Example rating", organization)
        with patch(
            "crank.services.crawl_scheduler.crawl_company_profile",
            return_value=type("Result", (), {"errors": 0})(),
        ) as crawl:
            counts = plan_crawls(phase=PHASE_ORGANIZATIONS)
        self.assertEqual(counts["scheduled"], 1)
        crawl.assert_called_once()
        self.assertEqual(crawl.call_args.args[0].pk, org_source.pk)
        self.assertIsNotNone(crawl.call_args.kwargs["now"])
        org_source.refresh_from_db()
        self.assertIsNotNone(org_source.last_crawl_at)

    def test_non_numeric_result_error_count_is_an_error(self):
        from crank.models.organization import Organization

        organization = Organization.objects.create(name="Example", url="https://example.test")
        source = make_org_source("Malformed result", organization)
        counts = plan_crawls(
            dispatchers={"organization": lambda *args: type("Result", (), {"errors": "bad"})()}
        )
        self.assertEqual(counts["scheduled"], 1)
        self.assertEqual(counts["errors"], 1)
        source.refresh_from_db()
        self.assertIsNone(source.last_crawl_at)

    def test_missing_and_expired_timestamps_are_stale(self):
        now = timezone.now()
        self.assertTrue(is_stale(None, 24, now=now))
        self.assertTrue(is_stale(now - timedelta(hours=24), 24, now=now))
        self.assertFalse(is_stale(now - timedelta(hours=23), 24, now=now))

    @override_settings(
        ORGANIZATION_FRESHNESS_HOURS=24,
        CRAWL_MAX_SOURCES=10,
    )
    @patch("crank.services.crawl_scheduler.monitoring.record_event")
    def test_dispatches_stale_sources_and_emits_bounded_counts(self, event):
        from crank.models.organization import Organization

        organization = Organization.objects.create(name="Example", url="https://example.test")
        org_source = make_org_source("Example rating", organization)
        make_job("Example jobs")
        now = timezone.now()
        calls = []

        def crawl(source, observed_at):
            calls.append(("organization", source.pk, observed_at))
            return type("Result", (), {"errors": 0})()

        counts = plan_crawls(now=now, dispatchers={"organization": crawl})

        self.assertEqual(counts["scheduled"], 1)
        self.assertEqual(counts["stale"], 1)
        self.assertEqual(counts["errors"], 0)
        self.assertEqual(len(calls), 1)
        org_source.refresh_from_db()
        self.assertEqual(org_source.last_crawl_at, now)
        event.assert_called_once_with("crawl_planning", counts)

    @patch("crank.services.crawl_scheduler.monitoring.record_event")
    def test_not_stale_and_disabled_sources_are_skipped(self, event):
        from crank.models.organization import Organization

        organization = Organization.objects.create(name="Pending Org", url="https://pending.example.test")
        make_org_source("Pending", organization, approval_state=ApprovalState.PENDING)
        disabled_org = Organization.objects.create(name="Disabled Org", url="https://disabled.example.test")
        make_org_source("Disabled", disabled_org, enabled=False)
        fresh_org = Organization.objects.create(name="Fresh Org", url="https://fresh.example.test")
        fresh = make_org_source("Fresh", fresh_org, last_crawl_at=timezone.now())
        called = []

        counts = plan_crawls(
            dispatchers={"organization": lambda *args: called.append(args)}
        )

        self.assertEqual(counts["scheduled"], 0)
        self.assertEqual(counts["stale"], 0)
        self.assertEqual(counts["skipped"], 3)
        self.assertFalse(called)
        self.assertEqual(event.call_count, 1)
        fresh.refresh_from_db()
        self.assertIsNotNone(fresh.last_crawl_at)

    @patch("crank.services.crawl_scheduler.monitoring.record_event")
    def test_errors_are_isolated_and_failed_source_remains_stale(self, event):
        from crank.models.organization import Organization

        organization = Organization.objects.create(name="Example", url="https://example.test")
        source = make_org_source("Broken", organization)

        def broken(*args):
            raise RuntimeError("provider payload must not be logged")

        counts = plan_crawls(dispatchers={"organization": broken})

        self.assertEqual(counts["scheduled"], 1)
        self.assertEqual(counts["errors"], 1)
        source.refresh_from_db()
        self.assertIsNone(source.last_crawl_at)
        event.assert_called_once()
        self.assertNotIn("provider payload", str(event.call_args))

    @patch("crank.services.crawl_scheduler.monitoring.record_event")
    def test_source_and_deadline_limits_skip_remaining_stale_sources(self, event):
        from crank.models.organization import Organization

        organization = Organization.objects.create(name="First Org", url="https://first.example.test")
        make_org_source("First", organization)
        second_org = Organization.objects.create(name="Second Org", url="https://second.example.test")
        make_org_source("Second", second_org)
        called = []
        counts = plan_crawls(
            max_sources=1,
            deadline_seconds=0,
            dispatchers={"organization": lambda *args: called.append(args)},
        )
        self.assertEqual(counts["scheduled"], 0)
        self.assertEqual(counts["stale"], 2)
        self.assertEqual(counts["skipped"], 2)
        self.assertFalse(called)


class ScheduleCrawlsCommandTests(TestCase):
    @override_settings(AGENT_RUN_ENABLED=False, CRAWL_CRON_ENABLED=True)
    @patch("crank.management.commands.schedule_crawls.plan_crawls")
    def test_master_switch_disables_command(self, planner):
        self.assertEqual(call_command("schedule_crawls", stdout=None), 0)
        planner.assert_not_called()

    @override_settings(AGENT_RUN_ENABLED=True, CRAWL_CRON_ENABLED=True)
    @patch("crank.management.commands.schedule_crawls.plan_crawls")
    def test_database_singleton_records_overlap_without_dispatch(self, planner):
        from crank.models.agent_run import AgentRun

        AgentRun.objects.create(
            run_type=AgentRun.RunType.CRAWL_SCHEDULE,
            status=AgentRun.Status.RUNNING,
            started_at=timezone.now(),
        )

        self.assertEqual(call_command("schedule_crawls", stdout=None), 0)
        planner.assert_not_called()
        self.assertTrue(
            AgentRun.objects.filter(
                run_type=AgentRun.RunType.CRAWL_SCHEDULE,
                status=AgentRun.Status.SKIPPED,
            ).exists()
        )

    @override_settings(AGENT_RUN_ENABLED=True, CRAWL_CRON_ENABLED=True)
    @patch("crank.management.commands.schedule_crawls.plan_crawls", return_value={"scheduled": 0})
    def test_phase_and_limits_are_forwarded(self, planner):
        self.assertEqual(
            call_command(
                "schedule_crawls",
                "--phase",
                "jobs",
                "--max-sources",
                "2",
                "--deadline-seconds",
                "7",
                stdout=None,
            ),
            0,
        )
        planner.assert_called_once_with(phase="jobs", max_sources=2, deadline_seconds=7)

    @override_settings(AGENT_RUN_ENABLED=True, CRAWL_CRON_ENABLED=True)
    def test_jobs_phase_prints_noop_explanation(self):
        """--phase jobs keeps working but explains the single-owner change."""
        make_job("Example jobs")
        output = StringIO()
        self.assertEqual(
            call_command("schedule_crawls", "--phase", "jobs", stdout=output),
            0,
        )
        text = output.getvalue()
        self.assertIn("no-op", text)
        self.assertIn("run_job_pipeline", text)
