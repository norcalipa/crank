# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Existing admin/dashboard events survive the enum policy unchanged (issue #482).

Most admin tests mock ``record_event`` and so assert the pre-sanitization
input. These drive the real call sites and assert on what reaches the vendor.
"""

from unittest.mock import Mock, patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import TestCase

from crank.admin import (
    CapabilitySwitchAdmin,
    JobRetrievalOps,
    JobSourceCatalogAdmin,
)
from crank.admin_dashboard import JobRetrievalOperationsAdmin
from crank.models import CapabilitySwitch, JobSourceCatalog
from crank.models.agent_run import AgentRun


def _request(user):
    return type(
        "Request",
        (),
        {
            "user": user,
            "POST": {"confirm": "yes"},
            "method": "POST",
            "META": {"SCRIPT_NAME": ""},
            "_messages": Mock(),
            "session": {},
        },
    )()


class AdminEventsReachVendorUnchangedTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.staff = User.objects.create_user("ops", password="pw", is_staff=True)
        patcher = patch("newrelic.agent.record_custom_event")
        self.vendor = patcher.start()
        self.addCleanup(patcher.stop)

    def emitted(self, event_name=None):
        events = [call.args[1] for call in self.vendor.call_args_list]
        return [e for e in events if event_name in (None, e.get("event_name"))]

    def assert_no_other(self):
        for event in self.emitted():
            for key, value in event.items():
                self.assertNotEqual(value, "other", f"{key} rewritten in {event}")

    def test_job_source_state_actions_keep_their_action(self):
        source_admin = JobSourceCatalogAdmin(JobSourceCatalog, self.site)
        source = JobSourceCatalog.objects.create(
            name="S", adapter_key="a1", base_url="https://jobs.example.test"
        )
        qs = JobSourceCatalog.objects.filter(pk=source.pk)
        with patch.object(source_admin, "message_user"):
            for name in ("approve_sources", "block_sources", "enable_sources", "disable_sources"):
                getattr(source_admin, name)(_request(self.staff), qs)
        actions = [e["action"] for e in self.emitted() if "action" in e]
        self.assertEqual(actions, ["approve", "block", "enable", "disable"])
        self.assert_no_other()

    def test_capability_actions_keep_their_action(self):
        switch = CapabilitySwitch.objects.create(key="interactive_agent", enabled=True)
        switch_admin = CapabilitySwitchAdmin(CapabilitySwitch, self.site)
        with patch.object(switch_admin, "message_user"):
            switch_admin.disable_capabilities(
                _request(self.staff), CapabilitySwitch.objects.filter(pk=switch.pk)
            )
        self.assertEqual([e["action"] for e in self.emitted() if "action" in e], ["disable"])
        self.assert_no_other()

    def test_queue_skip_keeps_queue_action_and_overlap_reason(self):
        AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE, status=AgentRun.Status.RUNNING
        )
        dashboard = JobRetrievalOperationsAdmin(JobRetrievalOps, self.site)
        with patch.object(dashboard, "message_user"):
            dashboard.queue_pipeline_view(_request(self.staff))
        skipped = [e for e in self.emitted() if e.get("action") == "queue"]
        self.assertEqual(len(skipped), 1, self.emitted())
        self.assertEqual(skipped[0]["status"], "skipped")
        self.assertEqual(skipped[0]["reason_code"], "overlap_existing")
        self.assert_no_other()

    def test_queue_and_retry_dashboard_events_keep_their_action(self):
        dashboard = JobRetrievalOperationsAdmin(JobRetrievalOps, self.site)
        with patch.object(dashboard, "message_user"):
            dashboard.queue_pipeline_view(_request(self.staff))
        self.assertIn("queue_pipeline", [e.get("action") for e in self.emitted()])
        self.assert_no_other()
