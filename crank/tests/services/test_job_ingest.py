# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the single-owner job-source ingestion boundary (issue #462)."""

from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TestCase

from crank.agents.jobs.ingest import JobIngestResult
from crank.models.job import JobSourceCatalog
from crank.services.job_ingest import (
    SKIP_OVERLAP,
    JobSourceIngestion,
    JobSourcePolicyError,
    ingest_job_source,
)


def make_source(name="Example jobs", **values):
    defaults = {
        "adapter_key": "fixture-adapter",
        "base_url": "https://jobs.example.test",
        "approval_state": JobSourceCatalog.ApprovalState.APPROVED,
        "enabled": True,
    }
    defaults.update(values)
    return JobSourceCatalog.objects.create(name=name, **defaults)


class JobSourceIngestBoundaryTests(TestCase):
    def setUp(self):
        self.source = make_source()
        self.query = MagicMock(name="query")

    def test_success_ingests_once_and_releases_lock(self):
        result = JobIngestResult(ingested=2, updated=1)
        with patch(
            "crank.services.job_ingest.ingest_jobs", return_value=result
        ) as ingest, patch(
            "crank.services.job_ingest.acquire_named_advisory_lock",
            return_value=True,
        ) as acquire, patch(
            "crank.services.job_ingest.release_named_advisory_lock"
        ) as release:
            ingestion = ingest_job_source(self.source, query=self.query)
        self.assertFalse(ingestion.skipped)
        self.assertEqual(ingestion.reason, "")
        self.assertIs(ingestion.result, result)
        acquire.assert_called_once_with("job_source:{}".format(self.source.pk), timeout_seconds=0)
        release.assert_called_once_with("job_source:{}".format(self.source.pk))
        ingest.assert_called_once_with(self.source, self.query, adapter=None)

    def test_policy_rejects_unapproved_disabled_and_unconfigured_sources(self):
        self.source.approval_state = JobSourceCatalog.ApprovalState.PENDING
        self.source.save(update_fields=["approval_state", "modified"])
        with self.assertRaises(JobSourcePolicyError):
            ingest_job_source(self.source, query=self.query)

        self.source.approval_state = JobSourceCatalog.ApprovalState.APPROVED
        self.source.enabled = False
        self.source.save(update_fields=["approval_state", "enabled", "modified"])
        with self.assertRaises(JobSourcePolicyError):
            ingest_job_source(self.source, query=self.query)

        self.source.enabled = True
        self.source.adapter_key = " "
        self.source.save(update_fields=["enabled", "adapter_key", "modified"])
        with self.assertRaises(JobSourcePolicyError):
            ingest_job_source(self.source, query=self.query)

    @patch("crank.services.job_ingest.monitoring.record_event")
    @patch("crank.services.job_ingest.ingest_jobs")
    def test_lock_held_skips_with_sanitized_reason(self, ingest, event):
        """A second path that loses the per-source lock skips, never fetches."""
        with patch(
            "crank.services.job_ingest.acquire_named_advisory_lock",
            return_value=False,
        ):
            ingestion = ingest_job_source(self.source, query=self.query)
        self.assertTrue(ingestion.skipped)
        self.assertEqual(ingestion.reason, SKIP_OVERLAP)
        self.assertIsNone(ingestion.result)
        ingest.assert_not_called()
        event.assert_called_once_with(
            "source_stage",
            {
                "stage": "job_ingest",
                "source_key": "fixture-adapter",
                "status": "skipped",
                "reason_code": SKIP_OVERLAP,
            },
        )

    def test_ingest_failure_still_releases_lock(self):
        with patch(
            "crank.services.job_ingest.ingest_jobs",
            side_effect=RuntimeError("boom"),
        ), patch(
            "crank.services.job_ingest.acquire_named_advisory_lock",
            return_value=True,
        ), patch(
            "crank.services.job_ingest.release_named_advisory_lock"
        ) as release:
            with self.assertRaises(RuntimeError):
                ingest_job_source(self.source, query=self.query)
        release.assert_called_once()

    def test_result_dataclass_shape(self):
        result = JobIngestResult()
        ingestion = JobSourceIngestion(result=result, skipped=False, reason="")
        self.assertIs(ingestion.result, result)
        self.assertFalse(ingestion.skipped)


@skipUnless(
    connection.vendor == "mysql",
    "Real GET_LOCK serialization requires MySQL; documented two-terminal drill in docs/runbook-crawl-scheduling.md",
)
class JobSourceIngestMySQLLockTests(TestCase):
    """Validate the per-source GET_LOCK guard on the production backend.

    On MySQL, two sessions contend for the same per-source lock: the loser
    skips with a sanitized reason. On other backends the partial unique
    constraints and upsert identity carry the guarantee, so this suite skips.
    Cross-process contention is proven by the two-terminal drill documented in
    docs/runbook-crawl-scheduling.md.
    """

    def test_second_holder_skips_while_first_holds_lock(self):
        from crank.models.agent_run import (
            acquire_named_advisory_lock,
            release_named_advisory_lock,
            source_lock_name,
        )

        source = make_source(name="MySQL lock drill")
        lock = source_lock_name(source)
        # Session A (this connection) holds the lock...
        self.assertTrue(acquire_named_advisory_lock(lock))
        # ...so the boundary (same backend, contended lock) must skip.
        ingestion = ingest_job_source(source, query=MagicMock())
        self.assertTrue(ingestion.skipped)
        self.assertEqual(ingestion.reason, SKIP_OVERLAP)
        # After release, the boundary ingests normally.
        release_named_advisory_lock(lock)
        with patch(
            "crank.services.job_ingest.ingest_jobs",
            return_value=JobIngestResult(),
        ):
            ingestion = ingest_job_source(source, query=MagicMock())
        self.assertFalse(ingestion.skipped)
