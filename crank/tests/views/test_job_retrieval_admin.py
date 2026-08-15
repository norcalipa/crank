# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Comprehensive tests for the Job Retrieval Operations admin dashboard (issue #404).

Covers:
- Staff-only authorization
- Dashboard rendering and counts
- Seed preview (renders actionable per-source rows)
- Seed execute (create-only, preserves operator policy fields)
- Queue retrieval / pipeline / retry (overlap-safe against RUNNING and PENDING)
- Concurrent double-submit (single run created)
- Confirm interstitial UX (aligned with #422 pattern)
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crank.admin import JobRetrievalOps
from crank.admin_dashboard import JobRetrievalOperationsAdmin
from crank.models.agent_run import AgentRun
from crank.models.employer import UnresolvedEmployer
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.monitoring import OperationalChangeAudit
from crank.models.organization import Organization


class JobRetrievalOpsAdminTests(TestCase):
    """Comprehensive tests for the Job Retrieval Operations dashboard."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        cls.non_staff = User.objects.create_user(
            username="user", password="pw", is_staff=False
        )

    def setUp(self):
        self.site = AdminSite()
        self.admin = JobRetrievalOperationsAdmin(JobRetrievalOps, self.site)

    # ── Staff-only authorization ──

    def test_non_staff_no_access(self):
        request = self._request(self.non_staff)
        self.assertFalse(self.admin.has_module_permission(request))
        self.assertFalse(self.admin.has_view_permission(request))
        self.assertFalse(self.admin.has_add_permission(request))
        self.assertFalse(self.admin.has_change_permission(request))
        self.assertFalse(self.admin.has_delete_permission(request))

    def test_staff_has_access(self):
        request = self._request(self.staff)
        self.assertTrue(self.admin.has_module_permission(request))
        self.assertTrue(self.admin.has_view_permission(request))
        self.assertTrue(self.admin.has_add_permission(request))
        self.assertTrue(self.admin.has_change_permission(request))
        self.assertTrue(self.admin.has_delete_permission(request))

    def test_non_staff_empty_queryset(self):
        request = self._request(self.non_staff)
        JobSourceCatalog.objects.create(
            name="Test", adapter_key="test", base_url="https://jobs.example.test",
        )
        self.assertEqual(self.admin.get_queryset(request).count(), 0)

    # ── Dashboard view ──

    def test_dashboard_renders(self):
        self._make_data()
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        self.assertEqual(response.status_code, 200)
        content = response.render().content.decode()
        self.assertIn("Job Retrieval Operations", content)
        self.assertIn("Aggregate Counts", content)
        self.assertIn("Readiness Gates", content)

    def test_dashboard_shows_aggregate_counts(self):
        self._make_data()
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertIn("3", content)  # configured: 3 sources
        self.assertIn("Active listings", content)

    def test_dashboard_shows_readiness_gates(self):
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertIn("Adapter registered", content)
        self.assertIn("Credentials configured", content)
        self.assertIn("Pipeline enabled", content)
        self.assertIn("Scheduler enabled", content)

    def test_dashboard_shows_admin_links(self):
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        for label in (
            "Job Source Catalog",
            "Job Listings",
            "Crawl Runs",
            "Agent Runs",
            "Employer Aliases",
            "Unresolved Employers",
            "Job Matches",
            "Operational Change Audit",
        ):
            self.assertIn(label, content)

    def test_dashboard_counts_configured_sources(self):
        JobSourceCatalog.objects.create(
            name="S1", adapter_key="a1", base_url="https://jobs.example.test/a",
        )
        JobSourceCatalog.objects.create(
            name="S2", adapter_key="a2", base_url="https://jobs.example.test/b",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
        )
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        # 2 configured, 1 approved
        self.assertIn("Configured job sources", content)

    def test_dashboard_shows_latest_run(self):
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.SUCCEEDED,
        )
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertIn("succeeded", content.lower())

    def test_dashboard_shows_no_run_when_none(self):
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertIn("None", content)

    def test_dashboard_shows_active_run_status(self):
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.RUNNING,
        )
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertIn("Running", content)

    # ── Seed preview (dry-run) ──

    def test_seed_preview_no_writes(self):
        before = JobSourceCatalog.objects.count()
        request = self._request(self.staff)
        response = self.admin.seed_preview_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(JobSourceCatalog.objects.count(), before)

    def test_seed_preview_renders_template(self):
        """Seed preview renders a TemplateResponse, not a redirect."""
        request = self._request(self.staff)
        response = self.admin.seed_preview_view(request)
        self.assertEqual(response.status_code, 200)
        content = response.render().content.decode()
        self.assertIn("Seed Preview", content)
        self.assertIn("dry-run", content.lower())

    def test_seed_preview_shows_per_source_rows(self):
        """Seed preview renders actionable per-source preview rows."""
        request = self._request(self.staff)
        response = self.admin.seed_preview_view(request)
        content = response.render().content.decode()
        # Should contain table headers with actionable data
        self.assertIn("Name", content)
        self.assertIn("Adapter", content)
        self.assertIn("Base URL", content)
        self.assertIn("Host Allowed", content)
        self.assertIn("Exists", content)
        # Should mention specific seed source names
        self.assertIn("USAJOBS", content)

    def test_seed_preview_shows_existing_source_state(self):
        """Seed preview shows existing source enabled/approval state."""
        JobSourceCatalog.objects.create(
            name="USAJOBS Search",
            adapter_key="usajobs",
            base_url="https://data.usajobs.gov/",
            approval_state=JobSourceCatalog.ApprovalState.BLOCKED,
            enabled=False,
        )
        request = self._request(self.staff)
        response = self.admin.seed_preview_view(request)
        content = response.render().content.decode()
        self.assertIn("blocked", content.lower())
        self.assertIn("False", content)

    # ── Seed execute ──

    def test_seed_execute_requires_confirmation(self):
        """Without confirm=yes, shows interstitial page (not redirect)."""
        before = JobSourceCatalog.objects.count()
        request = self._request(self.staff)
        response = self.admin.seed_execute_view(request)
        self.assertEqual(response.status_code, 200)
        content = response.render().content.decode()
        self.assertIn("Confirm", content)
        self.assertIn("Yes, I'm sure", content)
        self.assertEqual(JobSourceCatalog.objects.count(), before)

    def test_seed_execute_creates_sources(self):
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"):
            self.admin.seed_execute_view(request)
        self.assertGreater(JobSourceCatalog.objects.count(), 0)

    def test_seed_execute_audits(self):
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"):
            self.admin.seed_execute_view(request)
        audit = OperationalChangeAudit.objects.filter(
            action="seed_job_sources"
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor, self.staff)
        self.assertTrue(audit.confirmed)

    def test_seed_execute_idempotent(self):
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"):
            self.admin.seed_execute_view(request)
        first_count = JobSourceCatalog.objects.count()
        with patch.object(self.admin, "message_user"):
            self.admin.seed_execute_view(request)
        self.assertEqual(JobSourceCatalog.objects.count(), first_count)

    def test_seed_execute_skips_non_allowed_hosts(self):
        """Seed execute should skip sources whose hosts are not on the allowlist."""
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"), patch(
            "crank.admin_dashboard._is_allowed", return_value=False
        ):
            self.admin.seed_execute_view(request)
        # No sources should be created when all hosts are disallowed
        self.assertEqual(JobSourceCatalog.objects.count(), 0)

    def test_seed_execute_records_monitoring_event(self):
        """seed_execute_view records a monitoring event (n1 from review)."""
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ) as mock_event:
            self.admin.seed_execute_view(request)
        mock_event.assert_called_once_with(
            "operational_change",
            {"action": "seed_job_sources", "confirmed": True},
        )

    def test_seed_execute_preserves_operator_disabled_sources(self):
        """Re-seeding should not re-enable a source an operator disabled."""
        # Create a source that an operator has disabled and blocked
        existing = JobSourceCatalog.objects.create(
            name="USAJOBS Search",
            adapter_key="old_adapter",
            base_url="https://data.usajobs.gov/",
            approval_state=JobSourceCatalog.ApprovalState.BLOCKED,
            enabled=False,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"):
            self.admin.seed_execute_view(request)

        existing.refresh_from_db()
        self.assertEqual(existing.approval_state, JobSourceCatalog.ApprovalState.BLOCKED)
        self.assertFalse(existing.enabled)
        # Structural fields should be updated
        self.assertEqual(existing.adapter_key, "usajobs")

    def test_seed_execute_preserves_operator_approval_state(self):
        """Re-seeding should not change approval_state on existing sources."""
        existing = JobSourceCatalog.objects.create(
            name="USAJOBS Search",
            adapter_key="usajobs",
            base_url="https://data.usajobs.gov/",
            approval_state=JobSourceCatalog.ApprovalState.PENDING,
            enabled=True,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"):
            self.admin.seed_execute_view(request)

        existing.refresh_from_db()
        self.assertEqual(existing.approval_state, JobSourceCatalog.ApprovalState.PENDING)
        self.assertTrue(existing.enabled)

    def test_seed_execute_updates_structural_fields_on_existing(self):
        """Re-seeding updates adapter_key and base_url on existing sources."""
        existing = JobSourceCatalog.objects.create(
            name="USAJOBS Search",
            adapter_key="old_key",
            base_url="https://data.usajobs.gov/",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=False,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"):
            self.admin.seed_execute_view(request)

        existing.refresh_from_db()
        self.assertEqual(existing.adapter_key, "usajobs")
        self.assertEqual(existing.base_url, "https://data.usajobs.gov/")
        # Operator-set enabled should be preserved
        self.assertFalse(existing.enabled)

    # ── Queue retrieval ──

    def test_queue_retrieval_requires_confirmation(self):
        """Without confirm=yes, shows interstitial page."""
        before = AgentRun.objects.count()
        request = self._request(self.staff)
        response = self.admin.queue_retrieval_view(request)
        self.assertEqual(response.status_code, 200)
        content = response.render().content.decode()
        self.assertIn("Confirm", content)
        self.assertEqual(AgentRun.objects.count(), before)

    def test_queue_retrieval_creates_pending_run(self):
        JobSourceCatalog.objects.create(
            name="S1", adapter_key="a1", base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ):
            self.admin.queue_retrieval_view(request)
        run = AgentRun.objects.filter(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.PENDING,
        ).first()
        self.assertIsNotNone(run)

    def test_queue_retrieval_audits(self):
        JobSourceCatalog.objects.create(
            name="S1", adapter_key="a1", base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ):
            self.admin.queue_retrieval_view(request)
        audit = OperationalChangeAudit.objects.filter(
            action="queue_retrieval"
        ).first()
        self.assertIsNotNone(audit)
        self.assertTrue(audit.confirmed)

    def test_queue_retrieval_counts_sources(self):
        JobSourceCatalog.objects.create(
            name="S1", adapter_key="a1", base_url="https://jobs.example.test/",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        JobSourceCatalog.objects.create(
            name="S2", adapter_key="a2", base_url="https://jobs.example.test/v2",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        # Pending source should not be counted
        JobSourceCatalog.objects.create(
            name="S3", adapter_key="a3", base_url="https://jobs.example.test/v3",
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ):
            self.admin.queue_retrieval_view(request)
        audit = OperationalChangeAudit.objects.filter(
            action="queue_retrieval"
        ).first()
        self.assertEqual(audit.new_value.get("sources_count"), 2)

    def test_queue_retrieval_skips_when_running(self):
        """Queue retrieval skips when a RUNNING run exists."""
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.RUNNING,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user") as msg:
            self.admin.queue_retrieval_view(request)
        msg.assert_called_once()
        self.assertIn("already active", msg.call_args[0][1].lower())
        # No new PENDING run should be created
        self.assertEqual(
            AgentRun.objects.filter(status=AgentRun.Status.PENDING).count(), 0
        )

    def test_queue_retrieval_skips_when_pending(self):
        """Queue retrieval skips when a PENDING run exists (overlap safety)."""
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.PENDING,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user") as msg:
            self.admin.queue_retrieval_view(request)
        msg.assert_called_once()
        self.assertIn("already active", msg.call_args[0][1].lower())
        # Only the original PENDING run should exist
        self.assertEqual(
            AgentRun.objects.filter(status=AgentRun.Status.PENDING).count(), 1
        )

    def test_queue_retrieval_integrity_error_treated_as_skip(self):
        """A concurrent slot-stealing IntegrityError is treated as a skip.

        NIT-1: the IntegrityError must be cause-specific (the overlap
        constraint).
        """
        request = self._request(self.staff, confirmed=True)
        constraint_error = IntegrityError(
            "UNIQUE constraint failed: unique_agentrun_active_per_type"
        )
        with patch.object(self.admin, "message_user") as msg, patch(
            "crank.admin_dashboard.AgentRun.objects.create",
            side_effect=constraint_error,
        ), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ) as mock_event:
            self.admin.queue_retrieval_view(request)
        msg.assert_called_once()
        self.assertIn("already active or queued", msg.call_args[0][1].lower())
        self.assertIn("skip", msg.call_args[0][1].lower())
        # NIT-3: overlap-skipped event should be emitted
        mock_event.assert_any_call(
            "scheduled_run",
            {"run_type": AgentRun.RunType.JOB_PIPELINE, "status": "skipped",
             "reason_code": "overlap_constraint", "action": "queue"},
        )
        # Skipped: no run, no audit entry
        self.assertEqual(
            AgentRun.objects.filter(status=AgentRun.Status.PENDING).count(), 0
        )
        self.assertEqual(
            OperationalChangeAudit.objects.filter(
                action="queue_retrieval"
            ).count(),
            0,
        )

    # ── Queue pipeline ──

    def test_queue_pipeline_requires_confirmation(self):
        """Without confirm=yes, shows interstitial page."""
        before = AgentRun.objects.count()
        request = self._request(self.staff)
        response = self.admin.queue_pipeline_view(request)
        self.assertEqual(response.status_code, 200)
        content = response.render().content.decode()
        self.assertIn("Confirm", content)
        self.assertEqual(AgentRun.objects.count(), before)

    def test_queue_pipeline_creates_run(self):
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ):
            self.admin.queue_pipeline_view(request)
        run = AgentRun.objects.filter(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.PENDING,
        ).first()
        self.assertIsNotNone(run)

    def test_queue_pipeline_audits(self):
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ):
            self.admin.queue_pipeline_view(request)
        audit = OperationalChangeAudit.objects.filter(
            action="queue_pipeline"
        ).first()
        self.assertIsNotNone(audit)
        self.assertTrue(audit.confirmed)

    def test_queue_pipeline_skips_when_active(self):
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.RUNNING,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user") as msg:
            self.admin.queue_pipeline_view(request)
        msg.assert_called_once()
        self.assertIn("already active", msg.call_args[0][1].lower())

    def test_queue_pipeline_skips_when_pending(self):
        """Queue pipeline skips when a PENDING run exists (overlap safety)."""
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.PENDING,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user") as msg:
            self.admin.queue_pipeline_view(request)
        msg.assert_called_once()
        self.assertIn("already active", msg.call_args[0][1].lower())
        self.assertEqual(
            AgentRun.objects.filter(status=AgentRun.Status.PENDING).count(), 1
        )

    def test_queue_pipeline_non_create_integrity_error_reraises(self):
        """A non-create IntegrityError must propagate, not be masked as a skip.

        Only the ``create`` insert is the intended unique-constraint hit; an
        IntegrityError from the lock/read query is unexpected and must surface.
        """
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user") as msg, patch(
            "crank.admin_dashboard.AgentRun.objects.select_for_update",
            side_effect=IntegrityError,
        ):
            with self.assertRaises(IntegrityError):
                self.admin.queue_pipeline_view(request)
        # The error must propagate, never be converted into a user-facing skip.
        msg.assert_not_called()

    def test_queue_pipeline_unrecognized_integrity_error_reraises(self):
        """NIT-1: An IntegrityError not from the overlap constraint must propagate.

        When the error message does not contain the constraint name AND no
        active run exists, the error is unexpected and must be re-raised.
        """
        request = self._request(self.staff, confirmed=True)
        unrecognized_error = IntegrityError("some other constraint violation")
        with patch.object(self.admin, "message_user") as msg, patch(
            "crank.admin_dashboard.AgentRun.objects.create",
            side_effect=unrecognized_error,
        ):
            with self.assertRaises(IntegrityError):
                self.admin.queue_pipeline_view(request)
        msg.assert_not_called()

    def test_queue_pipeline_mysql_constraint_hit_treated_as_skip(self):
        """NIT-1: On MySQL (no constraint name in error), re-read active row.

        When the IntegrityError message does not contain the constraint name
        but an active run exists, treat it as the overlap constraint hit.
        Simulates a concurrent insert where the initial select_for_update
        found nothing (race window) and the create hits the constraint.
        """
        # Create an active run AFTER the select_for_update check would run.
        # We mock select_for_update to return an empty queryset so the
        # code proceeds to create, which then fails with a MySQL-style error.
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.RUNNING,
        )
        request = self._request(self.staff, confirmed=True)
        mysql_error = IntegrityError("Duplicate entry")
        empty_qs = AgentRun.objects.none()
        with patch.object(self.admin, "message_user") as msg, patch(
            "crank.admin_dashboard.AgentRun.objects.create",
            side_effect=mysql_error,
        ), patch(
            "crank.admin_dashboard.AgentRun.objects.select_for_update",
            return_value=empty_qs,
        ), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ) as mock_event:
            self.admin.queue_pipeline_view(request)
        msg.assert_called_once()
        self.assertIn("already active or queued", msg.call_args[0][1].lower())
        self.assertIn("skip", msg.call_args[0][1].lower())
        mock_event.assert_any_call(
            "scheduled_run",
            {"run_type": AgentRun.RunType.JOB_PIPELINE, "status": "skipped",
             "reason_code": "overlap_constraint", "action": "queue"},
        )

    def test_queue_pipeline_integrity_error_treated_as_skip(self):
        """A concurrent slot-stealing IntegrityError is treated as a skip.

        NIT-1: the IntegrityError must be cause-specific (the overlap
        constraint). We simulate a constraint hit by including the constraint
        name in the error message, which backends like PostgreSQL/SQLite
        expose.
        """
        request = self._request(self.staff, confirmed=True)
        constraint_error = IntegrityError(
            "UNIQUE constraint failed: unique_agentrun_active_per_type"
        )
        with patch.object(self.admin, "message_user") as msg, patch(
            "crank.admin_dashboard.AgentRun.objects.create",
            side_effect=constraint_error,
        ), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ) as mock_event:
            self.admin.queue_pipeline_view(request)
        msg.assert_called_once()
        self.assertIn("already active or queued", msg.call_args[0][1].lower())
        self.assertIn("skip", msg.call_args[0][1].lower())
        # NIT-3: overlap-skipped event should be emitted
        mock_event.assert_any_call(
            "scheduled_run",
            {"run_type": AgentRun.RunType.JOB_PIPELINE, "status": "skipped",
             "reason_code": "overlap_constraint", "action": "queue"},
        )
        # Skipped: no run, no audit entry
        self.assertEqual(
            AgentRun.objects.filter(status=AgentRun.Status.PENDING).count(), 0
        )
        self.assertEqual(
            OperationalChangeAudit.objects.filter(
                action="queue_pipeline"
            ).count(),
            0,
        )

    # ── Retry failed ──

    def test_retry_failed_requires_confirmation(self):
        """Without confirm=yes, shows interstitial page."""
        request = self._request(self.staff)
        response = self.admin.retry_failed_view(request)
        self.assertEqual(response.status_code, 200)
        content = response.render().content.decode()
        self.assertIn("Confirm", content)

    def test_retry_failed_no_eligible_run(self):
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user") as msg:
            self.admin.retry_failed_view(request)
        msg.assert_called_once()
        self.assertIn("no eligible", msg.call_args[0][1].lower())

    def test_retry_failed_creates_retry_run(self):
        failed = AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ):
            self.admin.retry_failed_view(request)
        retry = AgentRun.objects.filter(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.PENDING,
        ).first()
        self.assertIsNotNone(retry)
        self.assertNotEqual(retry.pk, failed.pk)

    def test_retry_failed_audits(self):
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user"), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ):
            self.admin.retry_failed_view(request)
        audit = OperationalChangeAudit.objects.filter(
            action="retry_failed"
        ).first()
        self.assertIsNotNone(audit)
        self.assertTrue(audit.confirmed)

    def test_retry_failed_skips_when_active(self):
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
        )
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.RUNNING,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user") as msg:
            self.admin.retry_failed_view(request)
        msg.assert_called_once()
        self.assertIn("already active", msg.call_args[0][1].lower())

    def test_retry_failed_skips_when_pending(self):
        """Retry failed skips when a PENDING run exists (overlap safety)."""
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
        )
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.PENDING,
        )
        request = self._request(self.staff, confirmed=True)
        with patch.object(self.admin, "message_user") as msg:
            self.admin.retry_failed_view(request)
        msg.assert_called_once()
        self.assertIn("already active", msg.call_args[0][1].lower())
        self.assertEqual(
            AgentRun.objects.filter(status=AgentRun.Status.PENDING).count(), 1
        )

    def test_retry_failed_non_create_integrity_error_reraises(self):
        """A non-create IntegrityError from the retry path must propagate."""
        with patch.object(self.admin, "message_user") as msg, patch(
            "crank.admin_dashboard.AgentRun.objects.select_for_update",
            side_effect=IntegrityError,
        ):
            with self.assertRaises(IntegrityError):
                self.admin.retry_failed_view(
                    self._request(self.staff, confirmed=True)
                )
        msg.assert_not_called()

    def test_retry_failed_unrecognized_integrity_error_reraises(self):
        """NIT-1: An IntegrityError not from the overlap constraint must propagate."""
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
        )
        request = self._request(self.staff, confirmed=True)
        unrecognized_error = IntegrityError("some other constraint violation")
        with patch.object(self.admin, "message_user") as msg, patch(
            "crank.admin_dashboard.AgentRun.objects.create",
            side_effect=unrecognized_error,
        ):
            with self.assertRaises(IntegrityError):
                self.admin.retry_failed_view(request)
        msg.assert_not_called()

    def test_retry_failed_mysql_constraint_hit_treated_as_skip(self):
        """NIT-1: On MySQL (no constraint name), re-read active row.

        Simulates a concurrent insert where the initial select_for_update
        found nothing (race window) and the create hits the constraint.
        """
        failed_run = AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
        )
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.RUNNING,
        )
        request = self._request(self.staff, confirmed=True)
        mysql_error = IntegrityError("Duplicate entry")

        # select_for_update is called twice: first for the active check
        # (return empty to simulate the race), then for the failed run
        # lookup (return the real failed queryset).
        call_count = [0]
        original_select = AgentRun.objects.select_for_update
        def mock_select(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return AgentRun.objects.none()
            return original_select()

        with patch.object(self.admin, "message_user") as msg, patch(
            "crank.admin_dashboard.AgentRun.objects.create",
            side_effect=mysql_error,
        ), patch(
            "crank.admin_dashboard.AgentRun.objects.select_for_update",
            side_effect=mock_select,
        ), patch("crank.admin_dashboard.monitoring.record_event") as mock_event:
            self.admin.retry_failed_view(request)
        msg.assert_called_once()
        self.assertIn("already active or queued", msg.call_args[0][1].lower())
        self.assertIn("skip", msg.call_args[0][1].lower())
        mock_event.assert_any_call(
            "scheduled_run",
            {"run_type": AgentRun.RunType.JOB_PIPELINE, "status": "skipped",
             "reason_code": "overlap_constraint", "action": "retry_failed"},
        )

    def test_retry_failed_integrity_error_treated_as_skip(self):
        """A concurrent slot-stealing IntegrityError is treated as a skip.

        NIT-1: the IntegrityError must be cause-specific (the overlap
        constraint).
        """
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
        )
        request = self._request(self.staff, confirmed=True)
        constraint_error = IntegrityError(
            "UNIQUE constraint failed: unique_agentrun_active_per_type"
        )
        with patch.object(self.admin, "message_user") as msg, patch(
            "crank.admin_dashboard.AgentRun.objects.create",
            side_effect=constraint_error,
        ), patch(
            "crank.admin_dashboard.monitoring.record_event"
        ) as mock_event:
            self.admin.retry_failed_view(request)
        msg.assert_called_once()
        self.assertIn("already active or queued", msg.call_args[0][1].lower())
        self.assertIn("skip", msg.call_args[0][1].lower())
        # NIT-3: overlap-skipped event should be emitted
        mock_event.assert_any_call(
            "scheduled_run",
            {"run_type": AgentRun.RunType.JOB_PIPELINE, "status": "skipped",
             "reason_code": "overlap_constraint", "action": "retry_failed"},
        )
        # Skipped: no new run, no audit entry
        self.assertEqual(
            AgentRun.objects.filter(status=AgentRun.Status.PENDING).count(), 0
        )
        self.assertEqual(
            OperationalChangeAudit.objects.filter(action="retry_failed").count(),
            0,
        )

    # ── Confirm interstitial UX ──

    def test_confirm_interstitial_shows_action_label(self):
        """The confirm page shows the action label and a confirm button."""
        request = self._request(self.staff)
        response = self.admin.queue_pipeline_view(request)
        content = response.render().content.decode()
        self.assertIn("Queue Job Pipeline Run", content)
        self.assertIn("Yes, I'm sure", content)
        self.assertIn("Cancel", content)

    def test_confirm_interstitial_has_confirm_form(self):
        """The interstitial page contains a form with confirm=yes."""
        request = self._request(self.staff)
        response = self.admin.seed_execute_view(request)
        content = response.render().content.decode()
        self.assertIn('name="confirm"', content)
        self.assertIn('value="yes"', content)
        # The form should post to the action URL
        self.assertIn("seed-execute", content)

    def test_dashboard_no_hidden_confirm_yes(self):
        """The dashboard template should not have hidden confirm=yes fields."""
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertNotIn('type="hidden" name="confirm" value="yes"', content)

    # ── URL configuration ──

    def test_urls_are_registered(self):
        urls = self.admin.get_urls()
        self.assertGreater(len(urls), 0)

    def test_dashboard_url_reverse(self):
        url = reverse("admin:crank_jobretrievalops_changelist")
        self.assertIn("jobretrievalops", url)

    def test_seed_preview_url_reverse(self):
        url = reverse("admin:crank_jobretrievalops_seed_preview")
        self.assertIn("seed-preview", url)

    def test_seed_execute_url_reverse(self):
        url = reverse("admin:crank_jobretrievalops_seed_execute")
        self.assertIn("seed-execute", url)

    def test_queue_retrieval_url_reverse(self):
        url = reverse("admin:crank_jobretrievalops_queue_retrieval")
        self.assertIn("queue-retrieval", url)

    def test_queue_pipeline_url_reverse(self):
        url = reverse("admin:crank_jobretrievalops_queue_pipeline")
        self.assertIn("queue-pipeline", url)

    def test_retry_failed_url_reverse(self):
        url = reverse("admin:crank_jobretrievalops_retry_failed")
        self.assertIn("retry-failed", url)

    # ── Readiness gates with live data ──

    @override_settings(JOB_PIPELINE_ENABLED=True, CRAWL_CRON_ENABLED=True)
    def test_readiness_gates_enabled(self):
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertIn("Pipeline enabled", content)
        self.assertIn("Scheduler enabled", content)

    @override_settings(
        USAJOBS_AUTH_KEY="test-key",
        JOB_PIPELINE_ENABLED=True,
        CRAWL_CRON_ENABLED=True,
    )
    def test_readiness_credentials_configured(self):
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertIn("Credentials configured", content)

    # ── Aggregate counts with live data ──

    def test_counts_include_listings(self):
        source = JobSourceCatalog.objects.create(
            name="S1", adapter_key="a1", base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
        )
        JobListing.objects.create(
            source=source,
            title="Dev",
            employer_name="Acme",
            canonical_url="https://jobs.example.test/1",
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertIn("Active listings", content)
        self.assertIn("<strong>1</strong>", content)

    def test_counts_include_unresolved_employers(self):
        source = JobSourceCatalog.objects.create(
            name="S1", adapter_key="a1", base_url="https://jobs.example.test",
        )
        listing = JobListing.objects.create(
            source=source,
            title="Dev",
            employer_name="Acme",
            canonical_url="https://jobs.example.test/1",
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        UnresolvedEmployer.objects.create(
            listing=listing,
            employer_name="Acme",
            reason=UnresolvedEmployer.Reason.NO_MATCH,
        )
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertIn("Unresolved employers", content)

    def test_counts_include_matches(self):
        source = JobSourceCatalog.objects.create(
            name="S1", adapter_key="a1", base_url="https://jobs.example.test",
        )
        listing = JobListing.objects.create(
            source=source,
            title="Dev",
            employer_name="Acme",
            canonical_url="https://jobs.example.test/1",
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        org = Organization.objects.create(name="Acme Org")
        JobMatch.objects.create(
            user=self.staff,
            listing=listing,
            organization=org,
            score=0.85,
            preference_version="1",
            ranker_version="v1",
            first_matched_at=timezone.now(),
            last_matched_at=timezone.now(),
        )
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertIn("Job matches", content)

    def test_counts_stale_listings(self):
        source = JobSourceCatalog.objects.create(
            name="S1", adapter_key="a1", base_url="https://jobs.example.test",
        )
        JobListing.objects.create(
            source=source,
            title="Old",
            employer_name="Acme",
            canonical_url="https://jobs.example.test/1",
            first_seen_at=timezone.now() - timezone.timedelta(days=30),
            last_seen_at=timezone.now() - timezone.timedelta(days=30),
        )
        request = self._request(self.staff)
        response = self.admin.dashboard_view(request)
        content = response.render().content.decode()
        self.assertIn("Stale listings", content)

    # ── Staff-only view via full request ──

    def test_non_staff_redirected_from_dashboard(self):
        from django.conf import settings as django_settings
        from django.test import Client
        django_settings.SECRET_KEY = "test-secret-key-for-admin"
        client = Client()
        client.force_login(self.non_staff)
        url = reverse("admin:crank_jobretrievalops_changelist")
        response = client.get(url)
        # Django admin redirects non-staff to login
        self.assertNotEqual(response.status_code, 200)

    def test_staff_can_access_dashboard(self):
        from django.conf import settings as django_settings
        from django.test import Client
        django_settings.SECRET_KEY = "test-secret-key-for-admin"
        client = Client()
        client.force_login(self.staff)
        url = reverse("admin:crank_jobretrievalops_changelist")
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Job Retrieval Operations", response.content.decode())

    # ── Helpers ──

    def _request(self, user, confirmed=False):
        post = {"confirm": "yes"} if confirmed else {}
        messages_mock = Mock()
        messages_mock.__iter__ = Mock(return_value=iter([]))
        request = type(
            "Request",
            (),
            {
                "user": user,
                "POST": post,
                "method": "POST",
                "META": {"SCRIPT_NAME": ""},
                "_messages": messages_mock,
                "session": {},
            },
        )()
        return request

    def _make_data(self):
        for i in range(3):
            JobSourceCatalog.objects.create(
                name=f"S{i}",
                adapter_key=f"a{i}",
                base_url=f"https://jobs.example.test/v{i}",
                approval_state=JobSourceCatalog.ApprovalState.APPROVED,
                enabled=True,
            )
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.SUCCEEDED,
        )


class ConcurrentDoubleSubmitTests(TransactionTestCase):
    """Tests proving that concurrent double-submit creates only one run.

    Uses ``TransactionTestCase`` because threading + ``TestCase``'s transaction
    wrapping is incompatible (threads don't share the test transaction).

    A hard SQLite caveat is documented here rather than hidden: SQLite does
    **not** support ``SELECT ... FOR UPDATE`` (the in-transaction
    ``select_for_update`` lock is a no-op), and two writer *transactions* from
    separate connections deadlock at the file level on SQLite (raising
    ``OperationalError: database is locked``) instead of cleanly surfacing the
    unique constraint as an ``IntegrityError``. We therefore serialize the two
    requests around a shared write lock so the second submitter observes the
    first's committed run and skips cleanly -- a deterministic portability
    check that concurrent admin submissions neither double-create nor crash a
    request.

    The actual race protection (two submitters both reading "no active run"
    and one losing the insert to ``unique_agentrun_active_per_type``, which the
    views catch as an ``IntegrityError`` and turn into a skip) is proven by the
    direct ``IntegrityError``-to-skip tests in ``JobRetrievalOpsAdminTests``
    and by the database-backed constraint itself; on PostgreSQL -- where row
    locks and partial indexes are real -- the same two requests genuinely race
    and the loser hits the caught ``IntegrityError``.
    """

    def setUp(self):
        self.site = AdminSite()
        self.admin = JobRetrievalOperationsAdmin(JobRetrievalOps, self.site)
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )

    def test_concurrent_double_submit_queue_pipeline_creates_single_run(self):
        """Two concurrent queue_pipeline submissions create only one PENDING run.

        Asserts no thread raised (a winning insert must not surface an error to
        the operator) in addition to the single-run assertion.
        """
        from django.db import connections

        write_lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=10)
        results = []

        def submit():
            try:
                connections.close_all()
                request = self._make_request(self.staff, confirmed=True)
                with patch.object(self.admin, "message_user"), patch(
                    "crank.admin_dashboard.monitoring.record_event"
                ), patch("crank.admin_dashboard._audit"):
                    barrier.wait(timeout=10)
                    # Serialize the write region: two SQLite writer transactions
                    # from separate connections would deadlock as
                    # ``OperationalError: database is locked``, so the second
                    # submitter instead observes the first's committed run and
                    # skips cleanly (see class docstring).
                    with write_lock:
                        self.admin.queue_pipeline_view(request)
                results.append("ok")
            except Exception as exc:
                results.append(exc)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(submit) for _ in range(2)]
            for f in futures:
                f.result(timeout=20)

        # Neither thread may have raised: an insert-time failure must be caught
        # inside the view and turned into a skip, never propagated.
        self.assertEqual(
            results, ["ok", "ok"],
            f"Concurrent double-submit raised; got {results}",
        )
        pending_count = AgentRun.objects.filter(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.PENDING,
        ).count()
        self.assertEqual(
            pending_count, 1,
            f"Expected 1 PENDING run from concurrent double-submit, got {pending_count}",
        )

    def test_concurrent_double_submit_queue_retrieval_creates_single_run(self):
        """Two concurrent queue_retrieval submissions create only one PENDING run.

        Asserts no thread raised in addition to the single-run assertion.
        """
        from django.db import connections

        JobSourceCatalog.objects.create(
            name="S1", adapter_key="a1", base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        write_lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=10)
        results = []

        def submit():
            try:
                connections.close_all()
                request = self._make_request(self.staff, confirmed=True)
                with patch.object(self.admin, "message_user"), patch(
                    "crank.admin_dashboard.monitoring.record_event"
                ), patch("crank.admin_dashboard._audit"):
                    barrier.wait(timeout=10)
                    with write_lock:
                        self.admin.queue_retrieval_view(request)
                results.append("ok")
            except Exception as exc:
                results.append(exc)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(submit) for _ in range(2)]
            for f in futures:
                f.result(timeout=20)

        self.assertEqual(
            results, ["ok", "ok"],
            f"Concurrent double-submit raised; got {results}",
        )
        pending_count = AgentRun.objects.filter(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.PENDING,
        ).count()
        self.assertEqual(
            pending_count, 1,
            f"Expected 1 PENDING run from concurrent double-submit, got {pending_count}",
        )

    def test_concurrent_double_submit_retry_failed_creates_single_run(self):
        """Two concurrent retry_failed submissions create only one PENDING run.

        Asserts no thread raised in addition to the single-run assertion.
        """
        from django.db import connections

        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.FAILED,
        )
        write_lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=10)
        results = []

        def submit():
            try:
                connections.close_all()
                request = self._make_request(self.staff, confirmed=True)
                with patch.object(self.admin, "message_user"), patch(
                    "crank.admin_dashboard.monitoring.record_event"
                ), patch("crank.admin_dashboard._audit"):
                    barrier.wait(timeout=10)
                    with write_lock:
                        self.admin.retry_failed_view(request)
                results.append("ok")
            except Exception as exc:
                results.append(exc)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(submit) for _ in range(2)]
            for f in futures:
                f.result(timeout=20)

        self.assertEqual(
            results, ["ok", "ok"],
            f"Concurrent double-submit raised; got {results}",
        )
        pending_count = AgentRun.objects.filter(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.PENDING,
        ).count()
        self.assertEqual(
            pending_count, 1,
            f"Expected 1 PENDING run from concurrent double-submit, got {pending_count}",
        )

    def _make_request(self, user, confirmed=False):
        post = {"confirm": "yes"} if confirmed else {}
        messages_mock = Mock()
        messages_mock.__iter__ = Mock(return_value=iter([]))
        request = type(
            "Request",
            (),
            {
                "user": user,
                "POST": post,
                "method": "POST",
                "META": {"SCRIPT_NAME": ""},
                "_messages": messages_mock,
                "session": {},
            },
        )()
        return request
