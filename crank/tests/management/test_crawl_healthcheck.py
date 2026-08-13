# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for crawl_healthcheck management command."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from crank.models.job import JobListing, JobSourceCatalog


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

    @patch("crank.management.commands.crawl_healthcheck.monitoring.record_event")
    def test_no_emit_skips_telemetry(self, record):
        out = StringIO()
        with self.assertRaises(SystemExit) as ctx:
            call_command("crawl_healthcheck", "--no-emit", stdout=out)
        self.assertEqual(ctx.exception.code, 1)
        record.assert_not_called()
