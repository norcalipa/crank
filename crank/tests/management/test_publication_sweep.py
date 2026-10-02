# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""publication_sweep emits one bounded telemetry event per run (issue #482)."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from crank.models.publication import PublicationEvent

RECORD = "crank.management.commands.publication_sweep.monitoring.record_event"


class PublicationSweepTelemetryTests(TestCase):
    @patch(RECORD)
    def test_disabled_consumer_emits_disabled(self, record):
        with patch("crank.services.publication.consumer_enabled", return_value=False):
            call_command("publication_sweep", stdout=StringIO())
        record.assert_called_once_with("publication_sweep", {"status": "disabled"})

    @patch(RECORD)
    def test_completed_sweep_emits_counts_and_outbox_age(self, record):
        PublicationEvent.objects.create(
            target_type=list(PublicationEvent.TargetType)[0], target_id=1,
            event_kind=PublicationEvent.EventKind.CREATED,
        )
        with patch("crank.services.publication.consumer_enabled", return_value=True), patch(
            "crank.services.publication.sweep_pending",
            return_value={"scanned": 3, "processed": 2, "keys_deleted": 5},
        ):
            call_command("publication_sweep", stdout=StringIO())
        name, payload = record.call_args.args
        self.assertEqual(name, "publication_sweep")
        self.assertEqual(
            {k: payload[k] for k in ("status", "scanned", "processed", "keys_deleted")},
            {"status": "completed", "scanned": 3, "processed": 2, "keys_deleted": 5},
        )
        self.assertGreaterEqual(payload["outbox_oldest_age_seconds"], 0)

    @patch(RECORD)
    def test_empty_outbox_reports_zero_age(self, record):
        with patch("crank.services.publication.consumer_enabled", return_value=True):
            call_command("publication_sweep", stdout=StringIO())
        self.assertEqual(record.call_args.args[1]["outbox_oldest_age_seconds"], 0)

    @patch(RECORD)
    def test_failed_sweep_emits_failure_and_reraises(self, record):
        with patch("crank.services.publication.consumer_enabled", return_value=True), patch(
            "crank.services.publication.sweep_pending", side_effect=TimeoutError("secret")
        ):
            with self.assertRaises(TimeoutError):
                call_command("publication_sweep", stdout=StringIO())
        record.assert_called_once_with(
            "publication_sweep",
            {"status": "failed", "reason_code": "timeout", "failure_stage": "publication"},
        )
