# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""End-to-end crawl-to-recommendation staging smoke test (issue #375).

This module exercises the critical path from an approved data source through
crawl/ingest → employer resolution → preference-bearing user → persisted match,
using **only** fixture/fake data.  No live Firecrawl network calls are made.

Run locally::

    python manage.py pytest crank/tests/services/test_crawl_to_recommendation_smoke.py -v

In CI (offline-safe)::

    coverage run -m pytest crank/tests/services/test_crawl_to_recommendation_smoke.py

The test fails loudly if any wiring in the pipeline is broken: the source
must be approved, the adapter must ingest, the listing must be active, the
user must have meaningful preferences, and a match must be persisted and
visible through the model query used by the recommendation UI.
"""

from __future__ import annotations

from datetime import datetime, timezone

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from crank.agents.jobs.base import JobSourceQuery
from crank.agents.jobs.ingest import ingest_jobs
from crank.agents.jobs.match_persist import persist_matches
from crank.agents.jobs.matching import project_criteria
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG
from crank.models import AgentRun, JobMatch, JobSourceCatalog, UserPreference
from crank.models.employer import EmployerAlias
from crank.models.job import JobListing
from crank.models.organization import Organization


# ---------------------------------------------------------------------------
# Deterministic fixture data
# ---------------------------------------------------------------------------

def _fake_firecrawl_response() -> dict:
    """Return a deterministic Firecrawl-style response with one listing.

    The URL host ``jobs.example.test`` is code-approved in
    ``APPROVED_JOB_SOURCE_DOMAINS`` and never resolves to a real server.
    """
    return {
        "id": "crawl-smoke-1",
        "status": "completed",
        "data": [
            {
                "extract": {
                    "title": "Senior Backend Engineer",
                    "canonical_url": "https://jobs.example.test/jobs/smoke-001",
                    "employer": "Example Labs",
                    "location": "Remote",
                    "remote_status": True,
                    "compensation": {
                        "min": 140000,
                        "max": 180000,
                        "currency": "USD",
                        "interval": "year",
                    },
                    "description_excerpt": "Build and maintain safe APIs.",
                    "source_id": "smoke-001",
                    "status": "active",
                },
                "metadata": {"sourceURL": "https://jobs.example.test/careers"},
            },
        ],
    }


class FakeFirecrawlClient:
    """Minimal stand-in for :class:`FirecrawlClient` that returns fixture data.

    No HTTP request is made; the same deterministic payload is returned on
    every call, making the smoke test reproducible and offline-safe.
    """

    def __init__(self, response: dict | None = None) -> None:
        self._response = response or _fake_firecrawl_response()
        self.calls: list[tuple] = []

    def crawl_url(self, url: str, **kwargs):  # noqa: D401 - match real signature
        self.calls.append((url, kwargs))
        return self._response


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


@override_settings(
    FIRECRAWL_ENABLED=True,
    FIRECRAWL_API_KEY="fixture-secret",
    FIRECRAWL_MAX_PAGES=3,
    FIRECRAWL_MAX_LISTINGS=5,
    FIRECRAWL_CREDIT_BUDGET=5,
    AGENT_RUN_ENABLED=True,
    JOB_PIPELINE_ENABLED=True,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class CrawlToRecommendationSmokeTest(TestCase):
    """Exercise crawl → ingest → employer resolution → match → visibility.

    Every stage is driven by fakes/fixtures.  If any wiring breaks the
    corresponding assertion fails with a clear stage-identifying message.
    """

    def setUp(self) -> None:
        self.run = AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.RUNNING,
        )

    def test_crawl_ingest_match_visible(self) -> None:
        """The full pipeline surfaces a listing and a match for a user."""

        # ---- Stage 1: approved source ----
        from crank.agents.jobs.firecrawl import FirecrawlCareersAdapter

        source = JobSourceCatalog.objects.create(
            name="Smoke Test Careers",
            adapter_key="firecrawl-careers",
            base_url="https://jobs.example.test/careers",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        self.assertEqual(
            source.approval_state,
            JobSourceCatalog.ApprovalState.APPROVED,
            "Stage 1 failed: source must be approved",
        )
        self.assertTrue(source.enabled, "Stage 1 failed: source must be enabled")

        # ---- Stage 1b: organization + employer alias for resolution ----
        organization = Organization.objects.create(name="Example Labs")
        EmployerAlias.objects.create(
            organization=organization,
            kind=EmployerAlias.AliasKind.NAME,
            value="Example Labs",
            status=EmployerAlias.Status.APPROVED,
        )

        # ---- Stage 2: crawl/ingest via fake Firecrawl client ----
        fake_client = FakeFirecrawlClient()
        adapter = FirecrawlCareersAdapter(source, client=fake_client)
        result = ingest_jobs(source, JobSourceQuery(max_listings=10, max_pages=3), adapter=adapter)

        self.assertEqual(result.errors, 0, f"Stage 2 failed: ingest had errors: {result.error_summary}")
        self.assertEqual(result.ingested, 1, f"Stage 2 failed: expected 1 ingested, got {result.ingested}")
        self.assertGreaterEqual(result.items_seen, 1, "Stage 2 failed: no items seen from crawl")

        # ---- Stage 3: listing is present and active ----
        listing = JobListing.all_objects.filter(
            source=source, external_id="smoke-001"
        ).first()
        self.assertIsNotNone(listing, "Stage 3 failed: listing was not persisted")
        self.assertEqual(
            listing.status,
            JobListing.Status.ACTIVE,
            "Stage 3 failed: listing is not active",
        )
        self.assertEqual(
            listing.title,
            "Senior Backend Engineer",
            "Stage 3 failed: listing title mismatch",
        )
        self.assertEqual(
            listing.canonical_url,
            "https://jobs.example.test/jobs/smoke-001",
            "Stage 3 failed: listing URL mismatch",
        )
        # The default manager (ActiveJobListingManager) should surface it.
        visible = JobListing.objects.filter(source=source, external_id="smoke-001").first()
        self.assertIsNotNone(visible, "Stage 3 failed: active listing not visible via default manager")

        # ---- Stage 4: preference-bearing user ----
        user = User.objects.create_user(
            username="smoke_user", password="secret", is_active=True
        )
        UserPreference.objects.create(
            user=user,
            preferences={
                "compensation": {
                    "minimum_salary": 120000,
                    "currency": "USD",
                    "equity_minimum_percent": None,
                },
                "culture": [],
                "work_location": {"modes": ["remote"], "countries": [], "require_onsite": None},
                "geography": {"regions": [], "remote_friendly": True},
                "industry": [],
                "funding_stage": [],
                "vesting": {
                    "max_cliff_months": None,
                    "max_vesting_months": None,
                    "prefer_accelerated": None,
                },
                "exclusions": {
                    "companies": [],
                    "titles": [],
                    "industries": [],
                    "locations": [],
                },
                "priorities": {},
                "notes": "remote engineering",
            },
        )
        pref = UserPreference.objects.get(user=user)
        self.assertIsNotNone(pref, "Stage 4 failed: preference not created")

        # ---- Stage 5: match/recommendation visibility ----
        listings = list(JobListing.objects.filter(source=source))
        self.assertGreaterEqual(len(listings), 1, "Stage 5 failed: no active listings for matching")

        criteria = project_criteria(pref.preferences, pref.schema_version)
        match_count = persist_matches(user, listings, criteria, DEFAULT_CONFIG)
        self.assertGreaterEqual(
            match_count, 1, "Stage 5 failed: no matches persisted"
        )

        matches = JobMatch.objects.filter(user=user, listing=listing)
        self.assertTrue(matches.exists(), "Stage 5 failed: no match found for the smoke listing")
        match = matches.first()
        self.assertFalse(match.dismissed, "Stage 5 failed: match was unexpectedly dismissed")
        self.assertGreater(match.score, 0, "Stage 5 failed: match score is zero")
        self.assertEqual(
            match.preference_version, pref.schema_version,
            "Stage 5 failed: preference version mismatch on match",
        )

        # ---- Stage 6: the match is visible via the owner-scoped query ----
        owner_matches = JobMatch.objects.filter(
            user=user, dismissed=False
        ).select_related("listing")
        self.assertTrue(
            owner_matches.exists(),
            "Stage 6 failed: no undismissed matches visible to user",
        )
        self.assertEqual(
            owner_matches.first().listing.pk,
            listing.pk,
            "Stage 6 failed: visible match does not point to the smoke listing",
        )

        # ---- Stage 7: the user can navigate to the job URL ----
        self.assertEqual(
            owner_matches.first().listing.canonical_url,
            "https://jobs.example.test/jobs/smoke-001",
            "Stage 7 failed: match listing URL is not navigable",
        )

    def test_empty_source_fails_clearly(self) -> None:
        """An approved source with zero listings produces zero matches.

        This verifies the smoke test can detect a broken or empty crawl
        rather than silently passing.
        """
        from crank.agents.jobs.firecrawl import FirecrawlCareersAdapter

        source = JobSourceCatalog.objects.create(
            name="Empty Smoke Source",
            adapter_key="firecrawl-careers",
            base_url="https://jobs.example.test/careers",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        fake_client = FakeFirecrawlClient(response={"id": "crawl-empty", "status": "completed", "data": []})
        adapter = FirecrawlCareersAdapter(source, client=fake_client)
        result = ingest_jobs(source, JobSourceQuery(max_listings=10, max_pages=3), adapter=adapter)

        self.assertEqual(result.ingested, 0, "Empty source should produce zero ingested listings")
        self.assertEqual(
            JobListing.all_objects.filter(source=source).count(),
            0,
            "Empty source should leave zero listings in the database",
        )
