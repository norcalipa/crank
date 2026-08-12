# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import TestCase

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
