# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from crank.admin import (
    AgentRunAdmin,
    CapabilitySwitchAdmin,
    JobSourceCatalogAdmin,
    OperationalChangeAuditAdmin,
    SourceCatalogAdmin,
)
from crank.models import (
    AgentRun,
    CapabilitySwitch,
    JobSourceCatalog,
    OperationalChangeAudit,
    SourceCatalog,
)
from crank.models.source import ApprovalState
from crank.services.crawl_runs import CrawlRequestError


class MonitoringAdminTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.staff = User.objects.create_user(
            username="ops", password="pw", is_staff=True
        )
        self.non_staff = User.objects.create_user(username="user", password="pw")

    def request(self, user=None, confirmed=False):
        return type(
            "Request",
            (),
            {
                "user": user or self.staff,
                "POST": {"confirm": "yes"} if confirmed else {},
            },
        )()

    def test_run_and_audit_admin_are_staff_only(self):
        run_admin = AgentRunAdmin(AgentRun, self.site)
        audit_admin = OperationalChangeAuditAdmin(OperationalChangeAudit, self.site)
        request = self.request(self.non_staff)
        self.assertFalse(run_admin.has_view_permission(request))
        self.assertFalse(audit_admin.has_view_permission(request))
        self.assertEqual(run_admin.get_queryset(request).count(), 0)

    def test_capability_action_requires_confirmation(self):
        switch = CapabilitySwitch.objects.create(key="interactive_agent", enabled=True)
        switch_admin = CapabilitySwitchAdmin(CapabilitySwitch, self.site)
        with patch.object(switch_admin, "message_user") as message:
            switch_admin.disable_capabilities(
                self.request(confirmed=False),
                CapabilitySwitch.objects.filter(pk=switch.pk),
            )
        switch.refresh_from_db()
        self.assertTrue(switch.enabled)
        self.assertFalse(OperationalChangeAudit.objects.exists())
        message.assert_called_once()

    def test_capability_action_is_authorized_confirmed_and_audited(self):
        switch = CapabilitySwitch.objects.create(key="interactive_agent", enabled=True)
        switch_admin = CapabilitySwitchAdmin(CapabilitySwitch, self.site)
        with patch.object(switch_admin, "message_user"), patch(
            "crank.admin.monitoring.record_event"
        ) as event:
            switch_admin.disable_capabilities(
                self.request(confirmed=True),
                CapabilitySwitch.objects.filter(pk=switch.pk),
            )
        switch.refresh_from_db()
        audit = OperationalChangeAudit.objects.get(target_id="interactive_agent")
        self.assertFalse(switch.enabled)
        self.assertEqual(audit.actor, self.staff)
        self.assertEqual(audit.old_value, {"enabled": True})
        self.assertEqual(audit.new_value, {"enabled": False})
        self.assertTrue(audit.confirmed)
        event.assert_called_once()

    @patch("crank.admin.trigger_crawl")
    def test_job_crawl_action_requires_confirmation_and_triggers(self, trigger):
        source = JobSourceCatalog.objects.create(
            name="Crawl jobs",
            adapter_key="crawl-fixture",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, self.site)
        with patch.object(source_admin, "message_user") as message:
            source_admin.trigger_crawls(
                self.request(confirmed=False),
                JobSourceCatalog.objects.filter(pk=source.pk),
            )
        trigger.assert_not_called()
        message.assert_called_once()
        with patch.object(source_admin, "message_user"):
            source_admin.trigger_crawls(
                self.request(confirmed=True),
                JobSourceCatalog.objects.filter(pk=source.pk),
            )
        trigger.assert_called_once_with(
            source_key="crawl-fixture", source_type="job", requested_by=self.staff,
        )

    @patch("crank.admin.trigger_crawl")
    def test_rating_source_crawl_action_requires_confirmation_and_triggers(self, trigger):
        from crank.models.organization import Organization

        organization = Organization.objects.create(
            name="Crawl ratings", gives_ratings=True, status=1,
        )
        source = SourceCatalog.objects.create(
            organization=organization,
            name="Crawl ratings source",
            adapter_key="rating-crawl",
            base_url="https://ratings.example.test",
            approval_state=ApprovalState.APPROVED,
            enabled=True,
        )
        source_admin = SourceCatalogAdmin(SourceCatalog, self.site)
        with patch.object(source_admin, "message_user") as message:
            source_admin.trigger_crawls(
                self.request(confirmed=False),
                SourceCatalog.objects.filter(pk=source.pk),
            )
        trigger.assert_not_called()
        message.assert_called_once()
        with patch.object(source_admin, "message_user"):
            source_admin.trigger_crawls(
                self.request(confirmed=True),
                SourceCatalog.objects.filter(pk=source.pk),
            )
        trigger.assert_called_once_with(
            source_key="rating-crawl", source_type="organization", requested_by=self.staff,
        )

    @patch("crank.admin.trigger_crawl", side_effect=CrawlRequestError("blocked"))
    def test_job_crawl_action_reports_trigger_errors(self, trigger):
        source = JobSourceCatalog.objects.create(
            name="Error jobs",
            adapter_key="error-fixture",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, self.site)
        request = self.request(confirmed=True)
        with patch.object(source_admin, "message_user") as message:
            source_admin.trigger_crawls(
                request,
                JobSourceCatalog.objects.filter(pk=source.pk),
            )
        trigger.assert_called_once()
        message.assert_any_call(request, "Error jobs: blocked", level="error")

    def test_job_source_admin_is_staff_only_and_confirmed_action_audits(self):
        source = JobSourceCatalog.objects.create(
            name="Test jobs",
            adapter_key="test.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, self.site)
        self.assertFalse(source_admin.has_view_permission(self.request(self.non_staff)))
        with patch.object(source_admin, "message_user"):
            source_admin.disable_sources(
                self.request(confirmed=True),
                JobSourceCatalog.objects.filter(pk=source.pk),
            )
        source.refresh_from_db()
        audit = OperationalChangeAudit.objects.get(target_type="job_source")
        self.assertFalse(source.enabled)
        self.assertEqual(audit.old_value, {"approval_state": "approved", "enabled": True})
        self.assertEqual(audit.new_value, {"approval_state": "approved", "enabled": False})

    # --- SourceCatalogAdmin confirmation guard (lines 257-262) ---

    def test_source_catalog_action_requires_confirmation(self):
        """SourceCatalog admin action without confirm=yes should be a no-op."""
        from crank.models.organization import Organization
        org = Organization.objects.create(name="TestOrg")
        catalog = SourceCatalog.objects.create(
            organization=org,
            name="Test",
            adapter_key="test.v1",
            base_url="https://test.example",
            approval_state=ApprovalState.APPROVED,
            enabled=True,
        )
        catalog_admin = SourceCatalogAdmin(SourceCatalog, self.site)
        with patch.object(catalog_admin, "message_user") as msg:
            catalog_admin.approve_sources(
                self.request(confirmed=False),
                SourceCatalog.objects.filter(pk=catalog.pk),
            )
        catalog.refresh_from_db()
        self.assertTrue(catalog.enabled)  # unchanged
        self.assertFalse(OperationalChangeAudit.objects.exists())
        msg.assert_called_once()

    # --- JobSourceCatalogAdmin confirmation guard (lines 359-364) ---

    def test_job_source_action_requires_confirmation(self):
        """JobSourceCatalog admin action without confirm=yes should be a no-op."""
        source = JobSourceCatalog.objects.create(
            name="Test jobs",
            adapter_key="test.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, self.site)
        with patch.object(source_admin, "message_user") as msg:
            source_admin.disable_sources(
                self.request(confirmed=False),
                JobSourceCatalog.objects.filter(pk=source.pk),
            )
        source.refresh_from_db()
        self.assertTrue(source.enabled)  # unchanged
        self.assertFalse(OperationalChangeAudit.objects.exists())
        msg.assert_called_once()

    # --- JobSourceCatalogAdmin approve/block/enable branches (lines 371,373,375) ---

    def test_job_source_approve_action(self):
        source = JobSourceCatalog.objects.create(
            name="Test jobs",
            adapter_key="test.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.BLOCKED,
            enabled=False,
        )
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, self.site)
        with patch.object(source_admin, "message_user"), patch("crank.admin.monitoring.record_event"):
            source_admin.approve_sources(
                self.request(confirmed=True),
                JobSourceCatalog.objects.filter(pk=source.pk),
            )
        source.refresh_from_db()
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.APPROVED)

    def test_job_source_block_action(self):
        source = JobSourceCatalog.objects.create(
            name="Test jobs",
            adapter_key="test.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, self.site)
        with patch.object(source_admin, "message_user"), patch("crank.admin.monitoring.record_event"):
            source_admin.block_sources(
                self.request(confirmed=True),
                JobSourceCatalog.objects.filter(pk=source.pk),
            )
        source.refresh_from_db()
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.BLOCKED)

    def test_job_source_enable_action(self):
        source = JobSourceCatalog.objects.create(
            name="Test jobs",
            adapter_key="test.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=False,
        )
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, self.site)
        with patch.object(source_admin, "message_user"), patch("crank.admin.monitoring.record_event"):
            source_admin.enable_sources(
                self.request(confirmed=True),
                JobSourceCatalog.objects.filter(pk=source.pk),
            )
        source.refresh_from_db()
        self.assertTrue(source.enabled)

    # --- JobSourceCatalogAdmin action wrappers (lines 401, 409, 413) ---

    def test_job_source_enable_wrapper_calls_state_action(self):
        source = JobSourceCatalog.objects.create(
            name="Test jobs",
            adapter_key="test.v1",
            base_url="https://jobs.example.test",
            enabled=False,
        )
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, self.site)
        with patch.object(source_admin, "message_user"), patch("crank.admin.monitoring.record_event"):
            source_admin.enable_sources(
                self.request(confirmed=True),
                JobSourceCatalog.objects.filter(pk=source.pk),
            )
        source.refresh_from_db()
        self.assertTrue(source.enabled)
        self.assertTrue(OperationalChangeAudit.objects.filter(target_type="job_source").exists())

    def test_job_source_approve_wrapper_calls_state_action(self):
        source = JobSourceCatalog.objects.create(
            name="Test jobs",
            adapter_key="test.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.BLOCKED,
        )
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, self.site)
        with patch.object(source_admin, "message_user"), patch("crank.admin.monitoring.record_event"):
            source_admin.approve_sources(
                self.request(confirmed=True),
                JobSourceCatalog.objects.filter(pk=source.pk),
            )
        source.refresh_from_db()
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.APPROVED)

    def test_job_source_block_wrapper_calls_state_action(self):
        source = JobSourceCatalog.objects.create(
            name="Test jobs",
            adapter_key="test.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
        )
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, self.site)
        with patch.object(source_admin, "message_user"), patch("crank.admin.monitoring.record_event"):
            source_admin.block_sources(
                self.request(confirmed=True),
                JobSourceCatalog.objects.filter(pk=source.pk),
            )
        source.refresh_from_db()
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.BLOCKED)

    # --- CapabilitySwitchAdmin enable_capabilities (line 481) ---

    def test_capability_enable_action_is_authorized_confirmed_and_audited(self):
        switch = CapabilitySwitch.objects.create(key="interactive_agent", enabled=False)
        switch_admin = CapabilitySwitchAdmin(CapabilitySwitch, self.site)
        with patch.object(switch_admin, "message_user"), patch(
            "crank.admin.monitoring.record_event"
        ) as event:
            switch_admin.enable_capabilities(
                self.request(confirmed=True),
                CapabilitySwitch.objects.filter(pk=switch.pk),
            )
        switch.refresh_from_db()
        audit = OperationalChangeAudit.objects.get(target_id="interactive_agent")
        self.assertTrue(switch.enabled)
        self.assertEqual(audit.old_value, {"enabled": False})
        self.assertEqual(audit.new_value, {"enabled": True})
        self.assertTrue(audit.confirmed)
        event.assert_called_once()


class CapabilitySwitchClientAdminTest(TestCase):
    """E2E admin-client path locking the shared confirm mixin (issue #422)."""

    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_superuser(
            username="opswho", password="pw", is_staff=True
        )
        self.client.force_login(self.staff)

    def test_capability_switch_e2e_requires_confirmation_then_applies(self):
        switch = CapabilitySwitch.objects.create(key="interactive_agent", enabled=True)
        changelist = reverse("admin:crank_capabilityswitch_changelist")
        data = {
            "action": "disable_capabilities",
            "_selected_action": [str(switch.pk)],
            "index": "0",
            "select_across": "0",
        }
        # First POST (no confirm) -> confirmation page, no mutation.
        resp = self.client.post(changelist, data)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("Confirm admin action", content)
        self.assertIn("Disable selected capabilities", content)
        self.assertIn('name="confirm"', content)
        switch.refresh_from_db()
        self.assertTrue(switch.enabled)
        self.assertFalse(OperationalChangeAudit.objects.exists())
        # Confirmation POST with confirm=yes -> toggled + audited.
        with patch("crank.admin.monitoring.record_event"):
            resp2 = self.client.post(changelist, {**data, "confirm": "yes"})
        self.assertEqual(resp2.status_code, 302)
        switch.refresh_from_db()
        self.assertFalse(switch.enabled)
        audit = OperationalChangeAudit.objects.get(
            target_type="capability", action="disable"
        )
        self.assertTrue(audit.confirmed)
