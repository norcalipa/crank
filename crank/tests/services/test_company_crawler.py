# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for review-first company profile crawling."""

from datetime import timedelta

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
import pytest

from crank.admin import CompanyProfileObservationAdmin
from crank.models.company_profile import CompanyProfileObservation
from crank.models.employer import EmployerAlias
from crank.models.job import JobSourceCatalog
from crank.models.organization import Organization
from crank.models.score import Score, ScoreType
from crank.services.company_crawler import (
    EXTRACTION_VERSION,
    _brand,
    _domain,
    _locations,
    _text,
    _url,
    crawl_company_profile,
)
from crank.agents.jobs.errors import JobSourceDisabled, JobSourceNotApproved
from crank.agents.sources.errors import BlockedRedirectError, SchemaDriftError


class FakeClient:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def crawl_url(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return {"id": "profile-1", "status": "completed", "data": self.data}


def profile(**changes):
    value = {
        "career_url": "https://jobs.example.test/about",
        "company_domain": "example.test",
        "company_name": "Example Labs",
        "description": "Build useful APIs.",
        "locations": ["Remote"],
        "rto_evidence": "Remote first",
        "funding_evidence": "Series A",
        "public_status_evidence": "Private company",
        "logo_url": "https://jobs.example.test/logo.svg",
        "brand_metadata": {"color": "blue"},
    }
    value.update(changes)
    return {"extract": value, "metadata": {"sourceURL": value["career_url"]}}


@override_settings(FIRECRAWL_MAX_PAGES=3, FIRECRAWL_CREDIT_BUDGET=3)
class CompanyCrawlerTests(TestCase):
    def setUp(self):
        self.source = JobSourceCatalog.objects.create(
            name="Example careers",
            adapter_key="firecrawl-careers",
            base_url="https://jobs.example.test/careers",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )

    def test_creates_bounded_provenance_observation_and_emits_counts(self):
        client = FakeClient([profile()])
        observed_at = timezone.now() - timedelta(minutes=2)

        result = crawl_company_profile(self.source, client=client, now=observed_at)

        observation = CompanyProfileObservation.objects.get()
        self.assertEqual(result.observations, 1)
        self.assertEqual(result.total, 1)
        self.assertEqual(result.pending, 1)
        self.assertEqual(result.error_reasons, ())
        self.assertGreaterEqual(result.freshness_seconds, 119)
        self.assertEqual(observation.source_url, "https://jobs.example.test/about")
        self.assertEqual(observation.observed_domain, "example.test")
        self.assertEqual(observation.extraction_version, EXTRACTION_VERSION)
        self.assertEqual(observation.status, CompanyProfileObservation.Status.PENDING)
        self.assertEqual(client.calls[0][1]["max_pages"], 3)

    def test_repeated_crawl_is_idempotent(self):
        client = FakeClient([profile()])

        first = crawl_company_profile(self.source, client=client)
        second = crawl_company_profile(self.source, client=client)

        self.assertEqual(first.observations, 1)
        self.assertEqual(second.observations, 0)
        self.assertEqual(second.duplicates, 1)
        self.assertEqual(CompanyProfileObservation.objects.count(), 1)

    def test_conflicting_profile_enters_review_queue(self):
        organization = Organization.objects.create(
            name="Example Labs", url="https://example.test", public=False,
            funding_round=Organization.FundingRound.SERIES_A,
            rto_policy=Organization.RTOPolicy.REMOTE,
        )
        prior = CompanyProfileObservation.objects.create(
            organization=organization,
            source_url="https://jobs.example.test/about",
            observed_domain="example.test",
            observed_name="Example Labs",
            description="Human reviewed description",
            observed_at=timezone.now(),
            extraction_version="old.v1",
            fingerprint="prior-fingerprint",
        )

        result = crawl_company_profile(
            self.source, client=FakeClient([profile(description="Different description")])
        )

        observation = CompanyProfileObservation.objects.exclude(pk=prior.pk).get()
        self.assertEqual(result.conflicted, 1)
        self.assertEqual(observation.status, CompanyProfileObservation.Status.CONFLICTED)
        self.assertIn("description", observation.conflict_fields)
        organization.refresh_from_db()
        self.assertFalse(organization.public)
        self.assertEqual(organization.rto_policy, Organization.RTOPolicy.REMOTE)

    def test_stale_observation_is_conflicted_not_applied(self):
        organization = Organization.objects.create(name="Example Labs", url="https://example.test")
        newer = timezone.now()
        prior = CompanyProfileObservation.objects.create(
            organization=organization,
            source_url="https://jobs.example.test/about",
            observed_domain="example.test",
            observed_name="Example Labs",
            description="Newer fact",
            observed_at=newer,
            extraction_version="new.v1",
            fingerprint="newer-fingerprint",
        )

        result = crawl_company_profile(
            self.source,
            client=FakeClient([profile(description="Older fact")]),
            now=newer - timedelta(days=1),
        )

        observation = CompanyProfileObservation.objects.exclude(pk=prior.pk).get()
        self.assertEqual(result.conflicted, 1)
        self.assertIn("stale_observation", observation.conflict_fields)
        self.assertEqual(CompanyProfileObservation.objects.filter(status="auto_applied").count(), 0)

    def test_unsafe_and_malformed_profiles_are_rejected_without_rows(self):
        unsafe = crawl_company_profile(
            self.source, client=FakeClient([profile(career_url="https://evil.example/about")])
        )
        malformed = crawl_company_profile(
            self.source, client=FakeClient([{"extract": {"company_name": []}}])
        )

        self.assertEqual(unsafe.errors, 1)
        self.assertEqual(malformed.errors, 1)
        self.assertEqual(CompanyProfileObservation.objects.count(), 0)
        self.assertTrue(all("secret" not in reason.lower() for reason in unsafe.error_reasons))

    def test_unapproved_source_fails_closed(self):
        self.source.base_url = "https://evil.example/careers"
        with pytest.raises(BlockedRedirectError):
            crawl_company_profile(self.source, client=FakeClient([]))

    def test_source_policy_is_required(self):
        self.source.approval_state = JobSourceCatalog.ApprovalState.PENDING
        with pytest.raises(JobSourceNotApproved):
            crawl_company_profile(self.source, client=FakeClient([]))
        self.source.approval_state = JobSourceCatalog.ApprovalState.APPROVED
        self.source.enabled = False
        with pytest.raises(JobSourceDisabled):
            crawl_company_profile(self.source, client=FakeClient([]))
        self.source.enabled = True
        self.source.adapter_key = "other"
        with pytest.raises(BlockedRedirectError):
            crawl_company_profile(self.source, client=FakeClient([]))
        self.source.adapter_key = "firecrawl-careers"
        self.source.base_url = "https://sub.jobs.example.test/careers"
        with pytest.raises(BlockedRedirectError):
            crawl_company_profile(self.source, client=FakeClient([]))

    def test_helpers_reject_malformed_and_unbounded_data(self):
        self.assertEqual(_text(None, "field", 5), "")
        with pytest.raises(SchemaDriftError):
            _text(123, "field", 5)
        with pytest.raises(SchemaDriftError):
            _text("", "field", 5, required=True)
        with pytest.raises(SchemaDriftError):
            _text("abcdef", "field", 5)
        with pytest.raises(BlockedRedirectError):
            _url("https://[::1]/", allowed_hosts={"jobs.example.test"}, field_name="url")
        with pytest.raises(BlockedRedirectError):
            _url("https://[", allowed_hosts={"jobs.example.test"}, field_name="url")
        with pytest.raises(SchemaDriftError):
            _domain("https://user:pass@example.test/path")
        with pytest.raises(SchemaDriftError):
            _domain("invalid")
        self.assertEqual(_locations("Remote"), ["Remote"])
        self.assertEqual(_locations(None), [])
        with pytest.raises(SchemaDriftError):
            _locations({"bad": "value"})
        with pytest.raises(SchemaDriftError):
            _locations(["x"] * 51)
        self.assertEqual(_brand(None), {})
        with pytest.raises(SchemaDriftError):
            _brand([])
        with pytest.raises(SchemaDriftError):
            _brand({"nested": {"value": 1}})

    def test_approved_alias_deduplicates_company_identity(self):
        organization = Organization.objects.create(name="Canonical Labs", url="https://canonical.test")
        EmployerAlias.objects.create(
            organization=organization,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="alias.example.test",
            status=EmployerAlias.Status.APPROVED,
        )
        result = crawl_company_profile(
            self.source,
            client=FakeClient([profile(company_domain="alias.example.test", company_name="Alias Labs")]),
        )
        observation = CompanyProfileObservation.objects.get()
        self.assertEqual(result.auto_applied, 1)
        self.assertEqual(observation.organization, organization)
        self.assertEqual(observation.status, CompanyProfileObservation.Status.AUTO_APPLIED)

    def test_multiple_domain_matches_enter_review_queue(self):
        Organization.objects.create(name="First Labs", url="https://example.test")
        Organization.objects.create(name="Second Labs", url="https://example.test")
        result = crawl_company_profile(self.source, client=FakeClient([profile(company_name="First Labs")]))
        observation = CompanyProfileObservation.objects.get()
        self.assertEqual(result.conflicted, 1)
        self.assertEqual(observation.organization, None)
        self.assertIn("organization_identity", observation.conflict_fields)

    def test_identity_conflict_and_malformed_provider_response_are_safe(self):
        first = Organization.objects.create(name="Example Labs", url="https://example.test")
        Organization.objects.create(name="Other Labs", url="https://jobs.example.test")
        result = crawl_company_profile(
            self.source,
            client=FakeClient([profile(company_name="Other Labs")]),
        )
        observation = CompanyProfileObservation.objects.get()
        self.assertEqual(result.conflicted, 1)
        self.assertEqual(observation.organization, None)
        self.assertIn("organization_identity", observation.conflict_fields)
        self.assertEqual(first.name, "Example Labs")
        malformed = crawl_company_profile(self.source, client=FakeClient({"bad": "data"}))
        self.assertEqual(malformed.errors, 1)

    def test_default_client_and_invalid_response_are_safe(self):
        with override_settings(FIRECRAWL_API_KEY=""):
            result = crawl_company_profile(self.source)
        self.assertEqual(result.errors, 1)
        self.assertTrue(result.error_reasons)

    def test_profile_item_shapes_are_rejected(self):
        for item in ("not-an-object", {"extract": "not-an-object"},
                     {"extract": profile()["extract"], "metadata": "not-an-object"}):
            result = crawl_company_profile(self.source, client=FakeClient([item]))
            self.assertEqual(result.errors, 1)
            self.assertEqual(CompanyProfileObservation.objects.count(), 0)

    def test_crawl_never_changes_scores(self):
        organization = Organization.objects.create(name="Example Labs", url="https://example.test")
        score_type = ScoreType.objects.create(name="Culture")
        Score.objects.create(target=organization, source=organization, type=score_type, score=4.5)

        crawl_company_profile(self.source, client=FakeClient([profile()]))

        self.assertEqual(Score.objects.get(target=organization).score, 4.5)


class CompanyProfileAdminTests(TestCase):
    def test_review_action_changes_only_observation_review_state(self):
        user = User.objects.create_user(username="operator", password="password", is_staff=True)
        observation = CompanyProfileObservation.objects.create(
            source_url="https://jobs.example.test/about",
            observed_domain="example.test",
            observed_name="Example Labs",
            observed_at=timezone.now(),
            extraction_version=EXTRACTION_VERSION,
            fingerprint="admin-fingerprint",
        )
        request = RequestFactory().post("/admin/crank/companyprofileobservation/")
        request.user = user
        model_admin = CompanyProfileObservationAdmin(CompanyProfileObservation, AdminSite())

        model_admin.message_user = lambda *_args, **_kwargs: None
        model_admin.accept_observations(request, CompanyProfileObservation.objects.filter(pk=observation.pk))

        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)
        self.assertEqual(observation.reviewed_by, user)
        self.assertIsNotNone(observation.reviewed_at)

    def test_reject_and_conflict_review_actions(self):
        user = User.objects.create_user(username="operator2", password="password", is_staff=True)
        observation = CompanyProfileObservation.objects.create(
            source_url="https://jobs.example.test/about",
            observed_domain="example.test",
            observed_name="Example Labs",
            observed_at=timezone.now(),
            extraction_version=EXTRACTION_VERSION,
            fingerprint="admin-fingerprint-2",
        )
        request = RequestFactory().post("/admin/crank/companyprofileobservation/")
        request.user = user
        model_admin = CompanyProfileObservationAdmin(CompanyProfileObservation, AdminSite())
        model_admin.message_user = lambda *_args, **_kwargs: None
        model_admin.reject_observations(request, CompanyProfileObservation.objects.filter(pk=observation.pk))
        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.REJECTED)
        model_admin.conflict_observations(request, CompanyProfileObservation.objects.filter(pk=observation.pk))
        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.CONFLICTED)

    def test_model_review_validation_and_note(self):
        observation = CompanyProfileObservation.objects.create(
            source_url="https://jobs.example.test/about",
            observed_at=timezone.now(),
            extraction_version=EXTRACTION_VERSION,
            fingerprint="admin-fingerprint-3",
        )
        with pytest.raises(ValueError):
            observation.mark_reviewed(status=CompanyProfileObservation.Status.PENDING)
        observation.mark_reviewed(status=CompanyProfileObservation.Status.ACCEPTED, note="checked")
        observation.refresh_from_db()
        self.assertEqual(observation.admin_note, "checked")
        self.assertEqual(str(observation), "https://jobs.example.test/about [accepted]")
        anonymous = CompanyProfileObservation(
            source_url="https://jobs.example.test/about",
            observed_at=timezone.now(),
            extraction_version=EXTRACTION_VERSION,
        )
        self.assertEqual(str(anonymous), "https://jobs.example.test/about [pending]")
