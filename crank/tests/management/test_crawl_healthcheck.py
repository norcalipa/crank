# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for crawl_healthcheck management command."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db.utils import OperationalError
from django.test import TestCase
from django.utils import timezone

from crank.models.job import JobListing, JobSourceCatalog
from crank.services import inventory_health


def make_source(name="Test Source", **kwargs):
    defaults = {
        "adapter_key": "firecrawl-careers",
        "base_url": "https://jobs.example.test",
        "approval_state": JobSourceCatalog.ApprovalState.APPROVED,
        "enabled": True,
    }
    defaults.update(kwargs)
    return JobSourceCatalog.objects.create(name=name, **defaults)


def make_listing(source, title="Engineer"):
    now = timezone.now()
    return JobListing.all_objects.create(
        source=source,
        external_id=f"{title}-1",
        canonical_url=f"https://jobs.example.test/listings/{title}-1",
        employer_name="Example Corp",
        title=title,
        first_seen_at=now,
        last_seen_at=now,
        status=JobListing.Status.ACTIVE,
    )


class CrawlHealthcheckCommandTests(TestCase):
    """Exit codes, output, and telemetry for the inventory health probe."""

    def test_unhealthy_when_empty_and_exits_nonzero(self):
        out = StringIO()
        with self.assertRaises(SystemExit) as ctx:
            call_command("crawl_healthcheck", stdout=out)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("UNHEALTHY", out.getvalue())
        self.assertIn("no approved and enabled job sources", out.getvalue())

    def test_healthy_exits_zero(self):
        now = timezone.now()
        source = make_source("Healthy", last_crawl_at=now)
        make_listing(source)
        out = StringIO()
        result = call_command("crawl_healthcheck", stdout=out)
        self.assertIsNone(result)
        self.assertIn("healthy", out.getvalue())

    @patch("crank.management.commands.crawl_healthcheck.monitoring.record_event")
    def test_emits_inventory_health_event(self, record):
        out = StringIO()
        with self.assertRaises(SystemExit):
            call_command("crawl_healthcheck", stdout=out)
        record.assert_called_once()
        event_name, payload = record.call_args.args
        self.assertEqual(event_name, "inventory_health")
        self.assertEqual(payload["sources_total"], 0)
        self.assertEqual(payload["enabled_sources"], 0)
        self.assertFalse(payload["healthy"])

    @patch("crank.services.monitoring.newrelic.agent.record_custom_event")
    def test_emitted_event_excludes_operator_only_violations(self, record):
        # The command passes the full result dict; the monitoring allowlist
        # must strip the operator-only violations list before New Relic.
        out = StringIO()
        with self.assertRaises(SystemExit):
            call_command("crawl_healthcheck", stdout=out)
        record.assert_called_once()
        event_type, payload = record.call_args.args
        self.assertEqual(event_type, "CrankOperation")
        self.assertNotIn("violations", payload)
        self.assertFalse(payload["healthy"])
        self.assertEqual(payload["enabled_sources"], 0)

    @patch("crank.management.commands.crawl_healthcheck.monitoring.record_event")
    def test_no_emit_skips_telemetry(self, record):
        out = StringIO()
        with self.assertRaises(SystemExit) as ctx:
            call_command("crawl_healthcheck", "--no-emit", stdout=out)
        self.assertEqual(ctx.exception.code, 1)
        record.assert_not_called()

    @patch("crank.management.commands.crawl_healthcheck.monitoring.record_event")
    def test_no_emit_skips_telemetry_when_healthy(self, record):
        now = timezone.now()
        source = make_source("Healthy", last_crawl_at=now)
        make_listing(source)
        out = StringIO()
        result = call_command("crawl_healthcheck", "--no-emit", stdout=out)
        self.assertIsNone(result)
        self.assertIn("healthy", out.getvalue())
        record.assert_not_called()

    @patch("crank.management.commands.crawl_healthcheck.monitoring.record_event")
    def test_check_failure_emits_degraded_event_and_exits_nonzero(self, record):
        out = StringIO()
        err = StringIO()
        with patch.object(
            inventory_health,
            "check_inventory_health",
            side_effect=OperationalError("db down"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                call_command("crawl_healthcheck", stdout=out, stderr=err)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("inventory health check failed", err.getvalue())
        record.assert_called_once()
        event_name, payload = record.call_args.args
        self.assertEqual(event_name, "inventory_health")
        self.assertFalse(payload["healthy"])
        self.assertEqual(payload["reason_code"], "internal")

    @patch("crank.management.commands.crawl_healthcheck.monitoring.record_event")
    def test_check_failure_with_no_emit_skips_telemetry(self, record):
        out = StringIO()
        err = StringIO()
        with patch.object(
            inventory_health,
            "check_inventory_health",
            side_effect=OperationalError("db down"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                call_command(
                    "crawl_healthcheck", "--no-emit", stdout=out, stderr=err
                )
        self.assertEqual(ctx.exception.code, 1)
        record.assert_not_called()
