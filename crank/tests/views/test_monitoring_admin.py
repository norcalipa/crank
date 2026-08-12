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
)
from crank.models import (
    AgentRun,
    CapabilitySwitch,
    JobSourceCatalog,
    OperationalChangeAudit,
)


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
