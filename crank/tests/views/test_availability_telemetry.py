# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""availability_state telemetry for assistant-status and job-match status (issue #482)."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from crank.services import monitoring

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _availability(record):
    return [c.args[1] for c in record.call_args_list if c.args[0] == "availability_state"]


@override_settings(CACHES=LOCMEM, INTERACTIVE_AGENT_ENABLED=True, JOB_SEARCH_PROVIDER="demo", ENV="dev")
class AssistantStatusTelemetryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

    def tearDown(self):
        cache.clear()

    @patch("crank.views.assistant_status.monitoring.record_event")
    def test_miss_then_hit_and_signed_out_state(self, record):
        url = reverse("agent-assistant-status")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 200)
        events = _availability(record)
        self.assertEqual([e["cached"] for e in events], [False, True])
        self.assertEqual({e["surface"] for e in events}, {"assistant_status"})
        self.assertEqual({e["state"] for e in events}, {"signed_out"})

    @patch("crank.views.assistant_status.monitoring.record_event")
    def test_payload_has_no_cached_key(self, record):
        body = self.client.get(reverse("agent-assistant-status")).json()
        self.assertNotIn("cached", body)

    @override_settings(INTERACTIVE_AGENT_ENABLED=False, JOB_SEARCH_PROVIDER="orchestrator")
    @patch("crank.views.assistant_status.monitoring.record_event")
    def test_authenticated_state_is_registered_enum(self, record):
        self.client.force_login(User.objects.create_user("avail", password="pw"))
        self.client.get(reverse("agent-assistant-status"))
        state = _availability(record)[0]["state"]
        self.assertEqual(state, "replies_disabled")
        self.assertIn(state, monitoring.enum_values()["state"])


@override_settings(CACHES=LOCMEM)
class JobMatchStatusTelemetryTests(TestCase):
    @patch("crank.views.job_matches.monitoring.record_event")
    def test_state_event_emitted(self, record):
        client = Client()
        client.force_login(User.objects.create_user("jm", password="pw"))
        resp = client.get(reverse("job-match-status"))
        self.assertEqual(resp.status_code, 200)
        events = _availability(record)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["surface"], "job_matches")
        self.assertEqual(events[0]["state"], resp.json()["state"])
        self.assertIn(events[0]["state"], monitoring.enum_values()["state"])
