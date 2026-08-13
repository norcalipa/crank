# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for read-only job-source inventory health checks."""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from crank.models.crawl_run import CrawlRun
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


def make_listing(source, title="Software Engineer", status=JobListing.Status.ACTIVE):
    now = timezone.now()
    return JobListing.all_objects.create(
        source=source,
        external_id=f"{title}-1",
        canonical_url=f"https://jobs.example.test/listings/{title}-1",
        employer_name="Example Corp",
        title=title,
        first_seen_at=now,
        last_seen_at=now,
        status=status,
    )


def make_crawl_run(source, outcome, started_at, counts=None):
    return CrawlRun.objects.create(
        source_type=CrawlRun.SourceType.JOB,
        source_key=source.adapter_key,
        job_source=source,
        outcome=outcome,
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=1),
        counts=counts or {},
    )


class InventoryHealthTests(TestCase):
    """Signal computation for the inventory bootstrap/monitoring health check."""

    def test_empty_inventory_reports_no_enabled_sources(self):
        result = inventory_health.check_inventory_health()
        self.assertEqual(result["sources_total"], 0)
        self.assertEqual(result["enabled_sources"], 0)
        self.assertEqual(result["active_listings"], 0)
        self.assertFalse(result["healthy"])
        self.assertIn("no approved and enabled job sources", result["violations"])

    def test_healthy_inventory_with_listings(self):
        now = timezone.now()
        source = make_source("Healthy", last_crawl_at=now)
        make_listing(source)
        result = inventory_health.check_inventory_health(now=now)
        self.assertTrue(result["healthy"])
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["active_listings"], 1)
        self.assertEqual(result["enabled_sources"], 1)

    def test_zero_active_listings_when_sources_exist(self):
        now = timezone.now()
        make_source("Empty", last_crawl_at=now)
        result = inventory_health.check_inventory_health(now=now)
        self.assertIn("zero active listings", result["violations"])
        self.assertFalse(result["healthy"])

    def test_stale_source_is_flagged(self):
        now = timezone.now()
        make_source("Stale", last_crawl_at=now - timedelta(hours=25))
        result = inventory_health.check_inventory_health(now=now)
        self.assertEqual(result["stale_sources"], 1)
        self.assertIn("1 enabled source(s) are stale", result["violations"])

    def test_repeated_failures_are_flagged(self):
        now = timezone.now()
        source = make_source("Flaky", last_crawl_at=now)
        for i in range(3):
            make_crawl_run(
                source, CrawlRun.Outcome.FAILURE, now - timedelta(minutes=i + 1)
            )
        result = inventory_health.check_inventory_health(now=now)
        self.assertEqual(result["repeated_failure_sources"], 1)
        self.assertIn("1 source(s) with repeated crawl failures", result["violations"])

    def test_repeated_failures_require_consecutive_failures(self):
        now = timezone.now()
        source = make_source("Recovering", last_crawl_at=now)
        make_crawl_run(source, CrawlRun.Outcome.FAILURE, now - timedelta(minutes=3))
        make_crawl_run(source, CrawlRun.Outcome.SUCCESS, now - timedelta(minutes=2))
        make_crawl_run(source, CrawlRun.Outcome.FAILURE, now - timedelta(minutes=1))
        result = inventory_health.check_inventory_health(now=now)
        self.assertEqual(result["repeated_failure_sources"], 0)

    def test_fewer_than_threshold_failures_not_flagged(self):
        now = timezone.now()
        source = make_source("New", last_crawl_at=now)
        make_crawl_run(source, CrawlRun.Outcome.FAILURE, now - timedelta(minutes=2))
        make_crawl_run(source, CrawlRun.Outcome.FAILURE, now - timedelta(minutes=1))
        result = inventory_health.check_inventory_health(now=now)
        self.assertEqual(result["repeated_failure_sources"], 0)

    def test_collapsed_source_is_flagged(self):
        now = timezone.now()
        source = make_source("Collapsed", last_crawl_at=now)
        make_crawl_run(
            source,
            CrawlRun.Outcome.SUCCESS,
            now - timedelta(hours=1),
            counts={"listings_ingested": 5},
        )
        result = inventory_health.check_inventory_health(now=now)
        self.assertEqual(result["collapsed_sources"], 1)
        self.assertIn(
            "1 source(s) collapsed to zero active listings", result["violations"]
        )

    def test_unregistered_adapter_is_flagged(self):
        now = timezone.now()
        make_source("Unknown Adapter", last_crawl_at=now, adapter_key="no-such-adapter")
        result = inventory_health.check_inventory_health(now=now)
        self.assertEqual(result["unregistered_adapter_sources"], 1)
        self.assertIn(
            "1 enabled source(s) with an unregistered adapter", result["violations"]
        )

    def test_only_approved_and_enabled_sources_are_counted(self):
        now = timezone.now()
        make_source("Enabled", last_crawl_at=now)
        make_source(
            "Pending",
            last_crawl_at=now,
            approval_state=JobSourceCatalog.ApprovalState.PENDING,
        )
        make_source("Disabled", last_crawl_at=now, enabled=False)
        result = inventory_health.check_inventory_health(now=now)
        self.assertEqual(result["sources_total"], 3)
        self.assertEqual(result["enabled_sources"], 1)

    def test_adapter_registered_helper(self):
        self.assertTrue(inventory_health.adapter_registered("firecrawl-careers"))
        self.assertFalse(inventory_health.adapter_registered("no-such-adapter"))
        self.assertFalse(inventory_health.adapter_registered(""))

    def test_is_stale_and_freshness(self):
        now = timezone.now()
        self.assertTrue(inventory_health._is_stale(None, now))
        self.assertTrue(inventory_health._is_stale(now - timedelta(hours=24), now))
        self.assertFalse(inventory_health._is_stale(now - timedelta(hours=23), now))

    @override_settings(JOB_FRESHNESS_HOURS="not-an-int")
    def test_freshness_hours_falls_back_on_invalid_setting(self):
        self.assertEqual(inventory_health.freshness_hours(), 24)

    @override_settings(JOB_FRESHNESS_HOURS=-5)
    def test_freshness_hours_clamps_to_zero(self):
        self.assertEqual(inventory_health.freshness_hours(), 0)

    @override_settings(CRAWL_REPEATED_FAILURE_THRESHOLD=1)
    def test_repeated_failure_threshold_is_configurable(self):
        now = timezone.now()
        source = make_source("Single", last_crawl_at=now)
        make_crawl_run(source, CrawlRun.Outcome.TIMEOUT, now - timedelta(minutes=1))
        result = inventory_health.check_inventory_health(now=now)
        self.assertEqual(result["repeated_failure_sources"], 1)
