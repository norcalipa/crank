# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for freshness-aware crawl scheduling and command guardrails."""

from datetime import timedelta
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

    def test_default_dispatchers_are_used_for_each_phase(self):
        from crank.models.organization import Organization

        organization = Organization.objects.create(name="Example", url="https://example.test")
        org_source = make_org_source("Example rating", organization)
        job_source = make_job("Example jobs")
        with patch("crank.services.crawl_scheduler.crawl_company_profile", return_value=type("Result", (), {"errors": 0})()) as crawl, patch(
            "crank.services.crawl_scheduler.ingest_jobs", return_value=type("Result", (), {"errors": 0})()
        ) as ingest:
            counts = plan_crawls(phase=PHASE_ORGANIZATIONS)
            self.assertEqual(counts["scheduled"], 1)
            crawl.assert_called_once()
            self.assertEqual(crawl.call_args.args[0].pk, org_source.pk)
            self.assertIsNotNone(crawl.call_args.kwargs["now"])
            org_source.refresh_from_db()
            self.assertIsNotNone(org_source.last_crawl_at)

            counts = plan_crawls(phase=PHASE_JOBS)
            self.assertEqual(counts["scheduled"], 1)
            ingest.assert_called_once()
            query = ingest.call_args.args[1]
            self.assertEqual(query.max_listings, 100)
            self.assertEqual(query.max_pages, 10)
            job_source.refresh_from_db()
            self.assertIsNotNone(job_source.last_crawl_at)

    def test_non_numeric_result_error_count_is_an_error(self):
        source = make_job("Malformed result")
        counts = plan_crawls(
            dispatchers={"jobs": lambda *args: type("Result", (), {"errors": "bad"})()}
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
        JOB_FRESHNESS_HOURS=24,
        CRAWL_MAX_SOURCES=10,
        CRAWL_MAX_JOB_LISTINGS=25,
        CRAWL_MAX_PAGES=2,
    )
    @patch("crank.services.crawl_scheduler.monitoring.record_event")
    def test_dispatches_stale_sources_and_emits_bounded_counts(self, event):
        from crank.models.organization import Organization

        organization = Organization.objects.create(name="Example", url="https://example.test")
        org_source = make_org_source("Example rating", organization)
        job_source = make_job("Example jobs")
        now = timezone.now()
        calls = []

        def crawl(source, observed_at):
            calls.append(("organization", source.pk, observed_at))
            return type("Result", (), {"errors": 0})()

        def ingest(source, observed_at, max_listings, max_pages):
            calls.append(("jobs", source.pk, max_listings, max_pages, observed_at))
            return type("Result", (), {"errors": 0})()

        counts = plan_crawls(
            now=now,
            dispatchers={"organization": crawl, "jobs": ingest},
        )

        self.assertEqual(counts["scheduled"], 2)
        self.assertEqual(counts["stale"], 2)
        self.assertEqual(counts["errors"], 0)
        self.assertEqual(len(calls), 2)
        org_source.refresh_from_db()
        job_source.refresh_from_db()
        self.assertEqual(org_source.last_crawl_at, now)
        self.assertEqual(job_source.last_crawl_at, now)
        event.assert_called_once_with("crawl_planning", counts)

    @patch("crank.services.crawl_scheduler.monitoring.record_event")
    def test_not_stale_and_disabled_sources_are_skipped(self, event):
        from crank.models.organization import Organization

        organization = Organization.objects.create(name="Example", url="https://example.test")
        make_org_source("Pending", organization, approval_state=ApprovalState.PENDING)
        make_job("Disabled", enabled=False)
        fresh = make_job("Fresh", last_crawl_at=timezone.now())
        called = []

        counts = plan_crawls(dispatchers={"jobs": lambda *args: called.append(args)})

        self.assertEqual(counts["scheduled"], 0)
        self.assertEqual(counts["stale"], 0)
        self.assertEqual(counts["skipped"], 3)
        self.assertFalse(called)
        self.assertEqual(event.call_count, 1)
        fresh.refresh_from_db()
        self.assertIsNotNone(fresh.last_crawl_at)

    @patch("crank.services.crawl_scheduler.monitoring.record_event")
    def test_errors_are_isolated_and_failed_source_remains_stale(self, event):
        source = make_job("Broken")

        def broken(*args):
            raise RuntimeError("provider payload must not be logged")

        counts = plan_crawls(dispatchers={"jobs": broken})

        self.assertEqual(counts["scheduled"], 1)
        self.assertEqual(counts["errors"], 1)
        source.refresh_from_db()
        self.assertIsNone(source.last_crawl_at)
        event.assert_called_once()
        self.assertNotIn("provider payload", str(event.call_args))

    @patch("crank.services.crawl_scheduler.monitoring.record_event")
    def test_source_and_deadline_limits_skip_remaining_stale_sources(self, event):
        make_job("First")
        make_job("Second")
        called = []
        counts = plan_crawls(
            max_sources=1,
            deadline_seconds=0,
            dispatchers={"jobs": lambda *args: called.append(args)},
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
