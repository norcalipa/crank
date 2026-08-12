# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for deterministic employer resolution and review queue."""

from __future__ import annotations

import pytest
from django.test import TestCase
from django.utils import timezone

from crank.agents.jobs.base import JobSourceQuery, JobSourceResult, RawJobListing
from crank.agents.jobs.employer import (
    EmployerResolution,
    reprocess_employer_alias,
    resolve_employer,
    sanitize_employer_text,
)
from crank.agents.jobs.ingest import ingest_jobs
from crank.models.employer import (
    EmployerAlias,
    UnresolvedEmployer,
    normalize_employer_domain,
    normalize_employer_identifier,
    normalize_employer_name,
)
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.organization import Organization


def make_org(name="Acme Corp", **kw):
    defaults = {"public": True}
    defaults.update(kw)
    return Organization.objects.create(name=name, **defaults)


def make_source(name="Test Source"):
    return JobSourceCatalog.objects.create(
        name=name,
        adapter_key="fixture-adapter",
        base_url="https://jobs.example.test",
        approval_state=JobSourceCatalog.ApprovalState.APPROVED,
        enabled=True,
    )


def make_listing(source, employer_name="Unknown Co", employer_domain="", employer_external_id="", **extra):
    now = timezone.now()
    raw = RawJobListing(
        external_id=extra.get("external_id", "job-1"),
        canonical_url=f"https://jobs.example.test/{extra.get('external_id', 'job-1')}",
        employer_name=employer_name,
        employer_domain=employer_domain,
        employer_external_id=employer_external_id,
        title="Software Engineer",
        first_seen_at=now,
        last_seen_at=now,
        source_metadata=extra.get("source_metadata", {}),
    )
    return JobListing.ingest(source, raw)


class StubAdapter:
    def __init__(self, listings=()):
        self.listings = tuple(listings)

    def fetch(self, query):
        return JobSourceResult(listings=self.listings, pages_fetched=1, items_seen=len(self.listings))


class NormalizeTests(TestCase):
    def test_normalize_employer_name_casefold_unicode(self):
        assert normalize_employer_name("ACME Corp") == "acme corp"
        assert normalize_employer_name("  Café   Münster  ") == "café münster"

    def test_normalize_employer_domain_strips_www_and_dot(self):
        assert normalize_employer_domain("WWW.Example.COM.") == "example.com"
        assert normalize_employer_domain("www.acme.org") == "acme.org"

    def test_normalize_employer_identifier_strips_tags(self):
        assert normalize_employer_identifier("<b>x</b>12345") == "x12345"

    def test_sanitize_employer_text_strips_html(self):
        assert sanitize_employer_text("<b>Bold</b> text") == "Bold text"

    def test_sanitize_employer_text_truncates(self):
        assert len(sanitize_employer_text("A" * 500, maximum=10)) == 10

    def test_sanitize_employer_text_none(self):
        assert sanitize_employer_text(None) == ""


class EmployerAliasTests(TestCase):
    def test_alias_save_normalizes_value(self):
        org = make_org()
        alias = EmployerAlias.objects.create(
            organization=org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="WWW.Acme.COM.",
        )
        assert alias.value == "acme.com"

    def test_alias_save_sanitizes_provenance(self):
        org = make_org()
        alias = EmployerAlias.objects.create(
            organization=org,
            kind=EmployerAlias.AliasKind.NAME,
            value="acme corp",
            provenance={"note": "<b>approved</b> by admin"},
        )
        assert "approved" in alias.provenance["note"]
        assert "<b>" not in alias.provenance["note"]

    def test_alias_save_handles_non_dict_provenance(self):
        org = make_org()
        alias = EmployerAlias.objects.create(
            organization=org,
            kind=EmployerAlias.AliasKind.NAME,
            value="acme corp",
            provenance="not-a-dict",
        )
        assert alias.provenance == {}

    def test_unique_constraint_blocks_duplicate_alias(self):
        org = make_org()
        EmployerAlias.objects.create(
            organization=org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="acme.com",
        )
        org2 = make_org(name="Other Corp")
        from django.db import IntegrityError
        with pytest.raises(IntegrityError):
            EmployerAlias.objects.create(
                organization=org2,
                kind=EmployerAlias.AliasKind.DOMAIN,
                value="acme.com",
            )

    def test_alias_str_representation(self):
        org = make_org()
        alias = EmployerAlias.objects.create(
            organization=org,
            kind=EmployerAlias.AliasKind.NAME,
            value="acme corp",
        )
        assert str(alias) == "name: acme corp"

    def test_alias_clean_normalizes_and_sanitizes(self):
        """clean() normalizes value and sanitizes provenance (lines 113-118)."""
        org = make_org()
        alias = EmployerAlias(
            organization=org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="WWW.Acme.COM.",
            provenance="<b>dirty</b>",
        )
        alias.clean()
        assert alias.value == "acme.com"
        assert alias.provenance == {}

    def test_unresolved_employer_str_representation(self):
        source = make_source()
        listing = make_listing(source, employer_name="Mystery Co")
        from crank.models.employer import UnresolvedEmployer
        ue = UnresolvedEmployer.objects.create(
            listing=listing,
            employer_name="Mystery Co",
            reason=UnresolvedEmployer.Reason.NO_MATCH,
        )
        assert str(ue) == f"{listing.id}: no_match"


class ResolveEmployerTests(TestCase):
    def setUp(self):
        self.source = make_source()
        self.org = make_org(name="Acme Corp")

    def test_resolve_by_external_id(self):
        EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.EXTERNAL_ID,
            value="ext-123",
            status=EmployerAlias.Status.APPROVED,
        )
        listing = make_listing(self.source, employer_name="Some Co", employer_external_id="ext-123")
        result = resolve_employer(listing, persist=False)
        assert result.resolved
        assert result.organization == self.org
        assert result.path == "external_id"

    def test_resolve_by_domain(self):
        EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="acme.com",
            status=EmployerAlias.Status.APPROVED,
        )
        listing = make_listing(self.source, employer_name="Some Co", employer_domain="acme.com")
        result = resolve_employer(listing, persist=False)
        assert result.resolved
        assert result.path == "domain"

    def test_resolve_by_name_alias(self):
        EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.NAME,
            value="acme corp",
            status=EmployerAlias.Status.APPROVED,
        )
        listing = make_listing(self.source, employer_name="ACME CORP")
        result = resolve_employer(listing, persist=False)
        assert result.resolved
        assert result.path == "name"

    def test_resolve_by_exact_name(self):
        listing = make_listing(self.source, employer_name="Acme Corp")
        result = resolve_employer(listing, persist=False)
        assert result.resolved
        assert result.path == "exact_name"

    def test_resolve_priority_external_id_over_domain(self):
        org2 = make_org(name="Other Corp")
        EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.EXTERNAL_ID,
            value="ext-1",
            status=EmployerAlias.Status.APPROVED,
        )
        EmployerAlias.objects.create(
            organization=org2,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="acme.com",
            status=EmployerAlias.Status.APPROVED,
        )
        listing = make_listing(self.source, employer_name="Some Co", employer_external_id="ext-1", employer_domain="acme.com")
        result = resolve_employer(listing, persist=False)
        assert result.organization == self.org
        assert result.path == "external_id"

    def test_resolve_priority_domain_over_name(self):
        org2 = make_org(name="Other Corp")
        EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="acme.com",
            status=EmployerAlias.Status.APPROVED,
        )
        EmployerAlias.objects.create(
            organization=org2,
            kind=EmployerAlias.AliasKind.NAME,
            value="some co",
            status=EmployerAlias.Status.APPROVED,
        )
        listing = make_listing(self.source, employer_name="Some Co", employer_domain="acme.com")
        result = resolve_employer(listing, persist=False)
        assert result.organization == self.org
        assert result.path == "domain"

    def test_ambiguous_returns_none(self):
        # Two organizations whose names normalize to the same value cause
        # ambiguity at the exact-name level. We use raw SQL to bypass the
        # unique constraint on Organization.name.
        from django.db import connection
        # self.org already exists as "Acme Corp" from setUp
        org_b = make_org(name="Beta Corp")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE crank_organization SET name = 'twin corp' WHERE id = %s",
                [self.org.pk],
            )
            cursor.execute(
                "UPDATE crank_organization SET name = 'TWIN CORP' WHERE id = %s",
                [org_b.pk],
            )
        listing = make_listing(self.source, employer_name="Twin Corp")
        result = resolve_employer(listing, persist=False)
        assert not result.resolved
        assert result.reason == UnresolvedEmployer.Reason.AMBIGUOUS
        assert len(result.candidates) == 2

    def test_no_match(self):
        listing = make_listing(self.source, employer_name="Unknown Co", employer_domain="unknown.com")
        result = resolve_employer(listing, persist=False)
        assert not result.resolved
        assert result.reason == UnresolvedEmployer.Reason.NO_MATCH

    def test_inactive_organization(self):
        org = make_org(name="Inactive Corp", status=0)
        EmployerAlias.objects.create(
            organization=org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="inactive.com",
            status=EmployerAlias.Status.APPROVED,
        )
        listing = make_listing(self.source, employer_name="Inactive Corp", employer_domain="inactive.com")
        result = resolve_employer(listing, persist=False)
        assert not result.resolved
        assert result.reason == UnresolvedEmployer.Reason.INACTIVE

    def test_not_public_organization(self):
        org = make_org(name="Private Corp", public=False)
        listing = make_listing(self.source, employer_name="Private Corp")
        result = resolve_employer(listing, persist=False)
        assert not result.resolved
        assert result.reason == UnresolvedEmployer.Reason.NOT_PUBLIC

    def test_pending_alias_not_used(self):
        # Use a name that doesn't match any existing org via exact name
        EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="acme.com",
            status=EmployerAlias.Status.PENDING,
        )
        listing = make_listing(self.source, employer_name="No Match Co", employer_domain="acme.com")
        result = resolve_employer(listing, persist=False)
        assert not result.resolved
        assert result.reason == UnresolvedEmployer.Reason.NO_MATCH

    def test_no_auto_create_organization(self):
        listing = make_listing(self.source, employer_name="Brand New Co", employer_domain="brandnew.com")
        result = resolve_employer(listing, persist=False)
        assert not result.resolved
        assert not Organization.objects.filter(name="Brand New Co").exists()


class PersistResolutionTests(TestCase):
    def setUp(self):
        self.source = make_source()
        self.org = make_org(name="Acme Corp")

    def test_persist_resolved_sets_organization(self):
        EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="acme.com",
            status=EmployerAlias.Status.APPROVED,
        )
        listing = make_listing(self.source, employer_name="Some Co", employer_domain="acme.com")
        result = resolve_employer(listing, persist=True)
        assert result.resolved
        listing.refresh_from_db()
        assert listing.organization == self.org

    def test_persist_unresolved_creates_unresolved_record(self):
        listing = make_listing(self.source, employer_name="Unknown Co")
        result = resolve_employer(listing, persist=True)
        assert not result.resolved
        assert UnresolvedEmployer.objects.filter(listing=listing, resolved=False).exists()

    def test_persist_resolved_clears_unresolved(self):
        # First: create an unresolved listing with no alias
        listing = make_listing(self.source, employer_name="Mystery Co", employer_domain="mystery.com")
        resolve_employer(listing, persist=True)
        assert UnresolvedEmployer.objects.filter(listing=listing, resolved=False).exists()
        # Now approve an alias that matches
        EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="mystery.com",
            status=EmployerAlias.Status.APPROVED,
        )
        resolve_employer(listing, persist=True)
        listing.refresh_from_db()
        assert listing.organization == self.org
        assert not UnresolvedEmployer.objects.filter(listing=listing, resolved=False).exists()


class ReprocessTests(TestCase):
    def setUp(self):
        self.source = make_source()
        self.org = make_org(name="Acme Corp")

    def test_reprocess_resolves_after_alias_approval(self):
        listing = make_listing(self.source, employer_name="Mystery Co", employer_domain="mystery.com")
        resolve_employer(listing, persist=True)
        assert UnresolvedEmployer.objects.filter(listing=listing, resolved=False).exists()

        alias = EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="mystery.com",
            status=EmployerAlias.Status.APPROVED,
        )
        count = reprocess_employer_alias(alias)
        assert count == 1
        listing.refresh_from_db()
        assert listing.organization == self.org
        assert not UnresolvedEmployer.objects.filter(listing=listing, resolved=False).exists()

    def test_reprocess_does_not_duplicate(self):
        EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="acme.com",
            status=EmployerAlias.Status.APPROVED,
        )
        listing = make_listing(self.source, employer_name="Some Co", employer_domain="acme.com")
        resolve_employer(listing, persist=True)
        listing_pk = listing.pk
        # Reprocess should not create duplicates
        alias = EmployerAlias.objects.get(value="acme.com")
        reprocess_employer_alias(alias)
        assert JobListing.all_objects.filter(source=self.source).count() == 1
        listing.refresh_from_db()
        assert listing.organization is not None

    def test_reprocess_skips_non_approved_alias(self):
        listing = make_listing(self.source, employer_name="Mystery Co", employer_domain="mystery.com")
        resolve_employer(listing, persist=True)
        alias = EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="mystery.com",
            status=EmployerAlias.Status.PENDING,
        )
        count = reprocess_employer_alias(alias)
        assert count == 0

    def test_reprocess_by_name_alias(self):
        listing = make_listing(self.source, employer_name="Mystery Co")
        resolve_employer(listing, persist=True)
        assert UnresolvedEmployer.objects.filter(listing=listing, resolved=False).exists()

        alias = EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.NAME,
            value="mystery co",
            status=EmployerAlias.Status.APPROVED,
        )
        count = reprocess_employer_alias(alias)
        assert count == 1
        listing.refresh_from_db()
        assert listing.organization == self.org

    def test_reprocess_by_external_id(self):
        listing = make_listing(self.source, employer_name="Mystery Co", employer_external_id="ext-456")
        resolve_employer(listing, persist=True)
        assert UnresolvedEmployer.objects.filter(listing=listing, resolved=False).exists()

        alias = EmployerAlias.objects.create(
            organization=self.org,
            kind=EmployerAlias.AliasKind.EXTERNAL_ID,
            value="ext-456",
            status=EmployerAlias.Status.APPROVED,
        )
        count = reprocess_employer_alias(alias)
        assert count == 1
        listing.refresh_from_db()
        assert listing.organization == self.org


class IngestIntegrationTests(TestCase):
    def test_ingest_triggers_resolution(self):
        source = make_source()
        org = make_org(name="Fixture Labs")
        EmployerAlias.objects.create(
            organization=org,
            kind=EmployerAlias.AliasKind.NAME,
            value="fixture labs",
            status=EmployerAlias.Status.APPROVED,
        )
        now = timezone.now()
        raw = RawJobListing(
            external_id="job-x",
            canonical_url="https://jobs.example.test/job-x",
            employer_name="Fixture Labs",
            title="Engineer",
            first_seen_at=now,
            last_seen_at=now,
        )
        result = ingest_jobs(source, JobSourceQuery(), adapter=StubAdapter([raw]))
        assert result.ingested == 1
        listing = JobListing.all_objects.get(source=source, external_id="job-x")
        assert listing.organization == org
