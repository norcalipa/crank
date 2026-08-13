# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for crawl_status management command."""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from crank.models.agent_run import AgentRun
from crank.models.crawl_run import CrawlRun
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


class CrawlStatusCommandTests(TestCase):
    """crawl_status output and flag behavior with fixture data."""

    def test_no_sources_message(self):
        out = StringIO()
        call_command("crawl_status", stdout=out)
        self.assertIn("No JobSourceCatalog rows found", out.getvalue())
        self.assertIn("seed_job_sources", out.getvalue())

    def test_shows_source_with_zero_listings(self):
        make_source("Empty Source")
        out = StringIO()
        call_command("crawl_status", stdout=out)
        self.assertIn("Empty Source", out.getvalue())
        self.assertIn("never", out.getvalue())
        self.assertIn("Total active listings: 0", out.getvalue())

    def test_shows_listing_count(self):
        source = make_source("Has Listings")
        make_listing(source, "Engineer I")
        make_listing(source, "Engineer II")
        out = StringIO()
        call_command("crawl_status", stdout=out)
        self.assertIn("Has Listings", out.getvalue())
        self.assertIn("Total active listings: 2", out.getvalue())

    def test_excludes_closed_by_default(self):
        source = make_source("Mixed Status")
        make_listing(source, "Active Job", status=JobListing.Status.ACTIVE)
        make_listing(source, "Closed Job", status=JobListing.Status.CLOSED)
        out = StringIO()
        call_command("crawl_status", stdout=out)
        self.assertIn("Total active listings: 1", out.getvalue())

    def test_include_closed_flag(self):
        source = make_source("Mixed Status")
        make_listing(source, "Active Job", status=JobListing.Status.ACTIVE)
        make_listing(source, "Closed Job", status=JobListing.Status.CLOSED)
        out = StringIO()
        call_command("crawl_status", "--include-closed", stdout=out)
        self.assertIn("Total active listings: 2", out.getvalue())

    def test_shows_last_crawl_time(self):
        source = make_source("Crawled Source")
        source.last_crawl_at = timezone.now() - timedelta(hours=2)
        source.save(update_fields=["last_crawl_at", "modified"])
        out = StringIO()
        call_command("crawl_status", stdout=out)
        self.assertIn("Crawled Source", out.getvalue())
        # The timestamp should appear (not "never")
        output = out.getvalue()
        self.assertNotIn("never", output.split("Crawled Source")[1].split("\n")[0])

    def test_shows_last_outcome_from_crawl_run(self):
        source = make_source("Crawled With Outcome")
        agent_run = AgentRun.objects.create(
            run_type=AgentRun.RunType.CRAWL,
            status=AgentRun.Status.SUCCEEDED,
            started_at=timezone.now() - timedelta(hours=1),
        )
        CrawlRun.objects.create(
            source_type=CrawlRun.SourceType.JOB,
            source_key="firecrawl-careers",
            job_source=source,
            agent_run=agent_run,
            started_at=agent_run.started_at,
            finished_at=timezone.now() - timedelta(minutes=50),
            outcome=CrawlRun.Outcome.SUCCESS,
        )
        out = StringIO()
        call_command("crawl_status", stdout=out)
        self.assertIn("Crawled With Outcome", out.getvalue())
        self.assertIn("success", out.getvalue())

    def test_shows_dash_when_no_crawl_run(self):
        make_source("Never Crawled")
        out = StringIO()
        call_command("crawl_status", stdout=out)
        self.assertIn("—", out.getvalue())

    def test_multiple_sources_are_sorted_by_name(self):
        make_source("Zebra Source")
        make_source("Alpha Source")
        out = StringIO()
        call_command("crawl_status", stdout=out)
        output_lines = out.getvalue().splitlines()
        # Find the data rows (after the header and separator)
        data_lines = [
            line
            for line in output_lines
            if "Alpha Source" in line or "Zebra Source" in line
        ]
        self.assertEqual(len(data_lines), 2)
        self.assertIn("Alpha Source", data_lines[0])
        self.assertIn("Zebra Source", data_lines[1])

    def test_disabled_source_shows_no(self):
        make_source("Disabled Source", enabled=False)
        out = StringIO()
        call_command("crawl_status", stdout=out)
        self.assertIn("Disabled Source", out.getvalue())
        # Find the line and check enabled column
        line = next(l for l in out.getvalue().splitlines() if "Disabled Source" in l)
        self.assertIn("no", line.lower())
