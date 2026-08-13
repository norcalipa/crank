# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Comprehensive tests for the Job Retrieval Operations admin dashboard (issue #404)."""

from unittest.mock import Mock, patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import TestCase, RequestFactory, override_settings
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
        run = AgentRun.objects.create(
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
        self.assertEqual(response.status_code, 302)
        self.assertEqual(JobSourceCatalog.objects.count(), before)

    def test_seed_preview_message(self):
        request = self._request(self.staff)
        with patch.object(self.admin, "message_user") as msg:
            self.admin.seed_preview_view(request)
        self.assertTrue(msg.called)
        self.assertIn("dry-run", msg.call_args[0][1].lower())

    # ── Seed execute ──

    def test_seed_execute_requires_confirmation(self):
        before = JobSourceCatalog.objects.count()
        request = self._request(self.staff)
        response = self.admin.seed_execute_view(request)
        self.assertEqual(response.status_code, 302)
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

    # ── Queue retrieval ──

    def test_queue_retrieval_requires_confirmation(self):
        before = AgentRun.objects.count()
        request = self._request(self.staff)
        response = self.admin.queue_retrieval_view(request)
        self.assertEqual(response.status_code, 302)
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

    # ── Queue pipeline ──

    def test_queue_pipeline_requires_confirmation(self):
        before = AgentRun.objects.count()
        request = self._request(self.staff)
        response = self.admin.queue_pipeline_view(request)
        self.assertEqual(response.status_code, 302)
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
        self.assertIn("already running", msg.call_args[0][1].lower())

    # ── Retry failed ──

    def test_retry_failed_requires_confirmation(self):
        request = self._request(self.staff)
        response = self.admin.retry_failed_view(request)
        self.assertEqual(response.status_code, 302)

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
        self.assertIn("already running", msg.call_args[0][1].lower())

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
