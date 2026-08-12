# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for bounded, auditable manual crawl runs."""

from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from crank.models import AgentRun, CrawlRun, JobSourceCatalog, OperationalChangeAudit, Organization, SourceCatalog
from crank.models.source import ApprovalState
from crank.services.crawl_runs import (
    CrawlRequestError,
    _execute,
    _outcome,
    _safe_counts,
    resolve_source,
    trigger_crawl,
)
from crank.agents.jobs.ingest import JobIngestResult


class CrawlRunTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("operator", is_staff=True)
        self.organization = Organization.objects.create(
            name="Rating Org", gives_ratings=True, status=1,
            url="https://ratings.example.test",
        )
        self.source = SourceCatalog.objects.create(
            name="Rating source", organization=self.organization,
            adapter_key="rating.v1", base_url="https://ratings.example.test",
            approval_state=ApprovalState.APPROVED, enabled=True,
        )
        self.job_source = JobSourceCatalog.objects.create(
            name="Jobs source", adapter_key="fixture-adapter",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED, enabled=True,
        )

    def test_resolve_accepts_numeric_primary_key(self):
        self.assertEqual(resolve_source(str(self.job_source.pk), "job"), self.job_source)

    def test_resolve_rejects_invalid_missing_and_ambiguous_keys(self):
        with self.assertRaises(CrawlRequestError):
            resolve_source("bad key", "job")
        with self.assertRaises(CrawlRequestError):
            resolve_source("missing", "job")
        JobSourceCatalog.objects.create(
            name="Second", adapter_key="fixture-adapter",
            base_url="https://jobs.example.test/second",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED, enabled=True,
        )
        with self.assertRaises(CrawlRequestError):
            resolve_source("fixture-adapter", "job")
        with self.assertRaises(CrawlRequestError):
            resolve_source("fixture-adapter", "unknown")

    def test_policy_rejects_unapproved_disabled_and_unconfigured_sources(self):
        self.source.approval_state = ApprovalState.PENDING
        self.source.save(update_fields=["approval_state", "modified"])
        with self.assertRaises(CrawlRequestError):
            trigger_crawl(source_key="rating.v1", source_type="organization")
        self.source.approval_state = ApprovalState.APPROVED
        self.source.enabled = False
        self.source.save(update_fields=["approval_state", "enabled", "modified"])
        with self.assertRaises(CrawlRequestError):
            trigger_crawl(source_key="rating.v1", source_type="organization")
        self.source.enabled = True
        self.source.adapter_key = ""
        self.source.save(update_fields=["enabled", "adapter_key", "modified"])
        with self.assertRaises(CrawlRequestError):
            trigger_crawl(source_key="Rating source", source_type="organization")
        self.source.adapter_key = "rating.v1"
        self.source.save(update_fields=["adapter_key", "modified"])

    def test_safe_counts_maps_dataclass_and_outcome_branches(self):
        result = JobIngestResult(ingested=2, updated=1, errors=1, items_seen=4)
        counts = _safe_counts(result)
        self.assertEqual(counts["listings_ingested"], 2)
        self.assertEqual(counts["listings_updated"], 1)
        self.assertEqual(counts["items_failed"], 1)
        self.assertEqual(counts["items_seen"], 4)
        self.assertEqual(_outcome(type("Result", (), {"errors": 0, "error_reasons": ("timeout",), "total": 1})()), "timeout")
        self.assertEqual(_outcome(type("Result", (), {"errors": 2, "error_reasons": (), "total": 0})()), "failure")

    @patch("crank.services.crawl_runs.crawl_company_profile")
    @patch("crank.services.crawl_runs.ingest_jobs")
    def test_execute_dispatches_by_source_type(self, ingest, company):
        _execute(self.source, "organization")
        company.assert_called_once_with(self.source)
        _execute(self.job_source, "job")
        ingest.assert_called_once()

    def test_policy_rejects_unknown_source_type(self):
        with self.assertRaises(CrawlRequestError):
            resolve_source("fixture-adapter", "invalid")

    def test_command_requires_confirmation_and_validates_policy(self):
        with self.assertRaises(CommandError):
            call_command("trigger_crawl", source_key="fixture-adapter", source_type="job")
        self.job_source.enabled = False
        self.job_source.save(update_fields=["enabled", "modified"])
        with self.assertRaises(CommandError):
            call_command(
                "trigger_crawl", source_key="fixture-adapter", source_type="job", confirm=True,
            )
        self.assertFalse(CrawlRun.objects.exists())

    @patch("crank.services.crawl_runs._execute")
    @patch("crank.services.crawl_runs.monitoring.record_event")
    def test_success_is_persisted_audited_and_telemetry_is_safe(self, event, execute):
        execute.return_value = type("Result", (), {
            "observations": 2, "errors": 0, "error_reasons": (), "total": 2,
        })()
        output = StringIO()
        call_command(
            "trigger_crawl", source_key="fixture-adapter", source_type="job",
            confirm=True, stdout=output,
        )
        run = CrawlRun.objects.get()
        self.assertEqual(run.outcome, CrawlRun.Outcome.SUCCESS)
        self.assertEqual(run.source_key, "fixture-adapter")
        self.assertEqual(run.requested_by, None)
        self.assertEqual(run.counts, {"items_seen": 2})
        self.assertEqual(AgentRun.objects.get().status, AgentRun.Status.SUCCEEDED)
        self.assertEqual(
            set(event.call_args_list[0].args[1]), {"run_id", "source_key"},
        )
        self.assertEqual(OperationalChangeAudit.objects.count(), 2)
        self.assertIn("success", output.getvalue())

    @patch("crank.services.crawl_runs._execute")
    def test_partial_and_failure_outcomes_keep_safe_summary(self, execute):
        execute.return_value = type("Result", (), {
            "observations": 1, "errors": 1, "error_reasons": ("SchemaDriftError (rejected)",), "total": 1,
        })()
        partial = trigger_crawl(source_key="fixture-adapter", source_type="job")
        self.assertEqual(partial.outcome, CrawlRun.Outcome.PARTIAL)
        self.assertNotIn("response", partial.error_summary.lower())

        execute.side_effect = TimeoutError("provider response body and token=secret")
        failed = trigger_crawl(source_key="rating.v1", source_type="organization")
        self.assertEqual(failed.outcome, CrawlRun.Outcome.TIMEOUT)
        self.assertNotIn("secret", failed.error_summary)
        self.assertNotIn("response body", failed.error_summary)
        self.assertEqual(failed.agent_run.status, AgentRun.Status.FAILED)

    @patch("crank.services.crawl_runs._execute")
    def test_duplicate_running_source_is_rejected(self, execute):
        execute.return_value = type("Result", (), {"observations": 0, "errors": 0, "error_reasons": (), "total": 0})()
        first = trigger_crawl(source_key="fixture-adapter", source_type="job")
        self.assertEqual(first.outcome, CrawlRun.Outcome.SUCCESS)
        CrawlRun.objects.create(
            source_type="job", source_key="fixture-adapter", job_source=self.job_source,
            outcome=CrawlRun.Outcome.RUNNING, started_at=timezone.now(),
        )
        with self.assertRaises(CrawlRequestError):
            trigger_crawl(source_key="fixture-adapter", source_type="job")

    def test_duration_and_string(self):
        pending = CrawlRun(source_type="job", source_key="fixture-adapter")
        self.assertIsNone(pending.duration)
        run = CrawlRun.objects.create(
            source_type="job", source_key="fixture-adapter", job_source=self.job_source,
            started_at=timezone.now(), finished_at=timezone.now(),
            outcome=CrawlRun.Outcome.SUCCESS,
        )
        self.assertIsNotNone(run.duration)
        self.assertIn("fixture-adapter", str(run))
