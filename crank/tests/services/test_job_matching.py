# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the preference-grounded matching service (issue #395)."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from crank.agents.jobs.matching import (
    ExclusionReason,
    project_criteria,
    rank_listing,
)
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.organization import Organization
from crank.models.preference import UserPreference, default_preferences
from crank.services.job_matching import (
    JobMatchResult,
    OrgMatchResult,
    _funding_label,
    _get_criteria,
    _org_excluded,
    _reasons_for_org,
    _reasons_from_factors,
    _rto_label,
    _score_organization_match,
    match_jobs,
    match_organizations,
    rank_listings_with_reasons,
)


def preferences(**overrides):
    result = default_preferences()
    for section, values in overrides.items():
        if isinstance(values, dict) and isinstance(result.get(section), dict):
            result[section].update(values)
        else:
            result[section] = values
    return result


def org(**values):
    defaults = {
        "rto_policy": "H",
        "funding_round": "A",
        "industry": "Software",
        "accelerated_vesting": True,
        "source_metadata": {},
        "avg_scores": lambda: [{"type__name": "culture", "avg_score": 4.0}],
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def listing(pk=1, organization="__default__", **values):
    defaults = {
        "pk": pk,
        "id": pk,
        "employer_name": "Acme",
        "title": "Senior Engineer",
        "location_text": "United States",
        "is_remote": True,
        "compensation_min": Decimal(120000),
        "compensation_max": Decimal(140000),
        "compensation_currency": "USD",
        "compensation_interval": "year",
        "description_excerpt": "Remote-first, collaborative team",
        "status": "active",
        "source_metadata": {},
        "last_seen_at": timezone.now(),
    }
    if organization is None:
        defaults["organization"] = None
    elif organization == "__default__":
        defaults["organization"] = org()
    else:
        defaults["organization"] = organization
    defaults.update(values)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Hard-filter correctness tests
# ---------------------------------------------------------------------------


class HardFilterTests(TestCase):
    """Test hard filters: public-only, RTO ceiling, exclusions."""

    def test_pre_ipo_excluded_when_require_public_company(self):
        """Pre-IPO companies are excluded when require_public_company is True."""
        criteria = project_criteria(
            preferences(compensation={"require_public_company": True}),
            2,
        )
        pre_ipo_org = org(funding_round="A")
        result = rank_listing(listing(organization=pre_ipo_org), criteria)
        assert result.excluded is True
        assert ExclusionReason.NOT_PUBLIC_COMPANY.value in result.exclusion_reasons

    def test_public_company_passes_when_require_public_company(self):
        """Public companies pass the public-only filter."""
        criteria = project_criteria(
            preferences(compensation={"require_public_company": True}),
            2,
        )
        public_org = org(funding_round="P")
        result = rank_listing(listing(organization=public_org), criteria)
        assert not result.excluded

    def test_rto_exceeds_maximum_excluded(self):
        """In-office orgs are excluded when max_in_office_days is below 5."""
        criteria = project_criteria(
            preferences(work_location={"max_in_office_days": 2}),
            2,
        )
        in_office_org = org(rto_policy="O")
        result = rank_listing(listing(organization=in_office_org), criteria)
        assert result.excluded is True
        assert ExclusionReason.RTO_EXCEEDS_MAXIMUM.value in result.exclusion_reasons

    def test_remote_passes_rto_ceiling(self):
        """Remote orgs pass any RTO ceiling."""
        criteria = project_criteria(
            preferences(work_location={"max_in_office_days": 0}),
            2,
        )
        remote_org = org(rto_policy="R")
        result = rank_listing(listing(organization=remote_org), criteria)
        assert not result.excluded

    def test_hybrid_passes_rto_ceiling_2(self):
        """Hybrid orgs (assumed 3 days) pass when max is >= 3."""
        criteria = project_criteria(
            preferences(work_location={"max_in_office_days": 3}),
            2,
        )
        hybrid_org = org(rto_policy="H")
        result = rank_listing(listing(organization=hybrid_org), criteria)
        assert not result.excluded

    def test_hybrid_excluded_when_max_below_3(self):
        """Hybrid orgs (assumed 3 days) excluded when max < 3."""
        criteria = project_criteria(
            preferences(work_location={"max_in_office_days": 2}),
            2,
        )
        hybrid_org = org(rto_policy="H")
        result = rank_listing(listing(organization=hybrid_org), criteria)
        assert result.excluded is True
        assert ExclusionReason.RTO_EXCEEDS_MAXIMUM.value in result.exclusion_reasons

    def test_no_filter_when_max_in_office_days_is_none(self):
        """No RTO filter when max_in_office_days is not set."""
        criteria = project_criteria(preferences(), 2)
        assert criteria.max_in_office_days is None
        in_office_org = org(rto_policy="O")
        result = rank_listing(listing(organization=in_office_org), criteria)
        assert not result.excluded

    def test_no_filter_when_require_public_is_none(self):
        """No public-only filter when require_public_company is not set."""
        criteria = project_criteria(preferences(), 2)
        assert criteria.require_public_company is None
        pre_ipo_org = org(funding_round="A")
        result = rank_listing(listing(organization=pre_ipo_org), criteria)
        assert not result.excluded


# ---------------------------------------------------------------------------
# Ranking stability tests
# ---------------------------------------------------------------------------


class RankingStabilityTests(TestCase):
    """Test deterministic ranking and tie-breaking."""

    def test_rank_listings_with_reasons_is_deterministic(self):
        criteria = project_criteria(preferences(), 2)
        listings = [listing(pk=20), listing(pk=3)]
        first = rank_listings_with_reasons(listings, criteria)
        second = rank_listings_with_reasons(list(reversed(listings)), criteria)
        assert [r.listing_id for r in first] == [r.listing_id for r in second]

    def test_results_are_sorted_by_score_desc_then_id_asc(self):
        criteria = project_criteria(preferences(), 2)
        results = rank_listings_with_reasons(
            [listing(pk=1), listing(pk=2), listing(pk=3)],
            criteria,
        )
        for i in range(len(results) - 1):
            if results[i].score == results[i + 1].score:
                assert results[i].listing_id < results[i + 1].listing_id
            else:
                assert results[i].score >= results[i + 1].score

    def test_excluded_listings_not_in_results(self):
        criteria = project_criteria(
            preferences(exclusions={"companies": ["Bad Co"]}),
            2,
        )
        results = rank_listings_with_reasons(
            [listing(pk=1), listing(pk=2, employer_name="Bad Co")],
            criteria,
        )
        assert len(results) == 1
        assert results[0].listing_id == 1


# ---------------------------------------------------------------------------
# Reason string tests
# ---------------------------------------------------------------------------


class ReasonStringTests(TestCase):
    """Test human-readable reason generation."""

    def test_public_company_reason(self):
        criteria = project_criteria(preferences(), 2)
        public_org = org(funding_round="P")
        results = rank_listings_with_reasons(
            [listing(organization=public_org)],
            criteria,
        )
        assert len(results) == 1
        assert "Public company" in results[0].reasons

    def test_remote_reason(self):
        criteria = project_criteria(preferences(), 2)
        remote_org = org(rto_policy="R")
        results = rank_listings_with_reasons(
            [listing(organization=remote_org)],
            criteria,
        )
        assert len(results) == 1
        assert "Remote" in results[0].reasons

    def test_hybrid_reason_with_days(self):
        criteria = project_criteria(preferences(), 2)
        hybrid_org = org(rto_policy="H")
        results = rank_listings_with_reasons(
            [listing(organization=hybrid_org)],
            criteria,
        )
        assert len(results) == 1
        assert any("Hybrid" in r for r in results[0].reasons)

    def test_score_reason(self):
        criteria = project_criteria(preferences(), 2)
        results = rank_listings_with_reasons([listing()], criteria)
        assert len(results) == 1
        assert any(r.startswith("Score ") for r in results[0].reasons)

    def test_reasons_bounded_to_six(self):
        criteria = project_criteria(preferences(), 2)
        results = rank_listings_with_reasons([listing()], criteria)
        assert len(results) == 1
        assert len(results[0].reasons) <= 6

    def test_salary_reason(self):
        criteria = project_criteria(
            preferences(compensation={"minimum_salary": 100000, "currency": "USD"}),
            2,
        )
        results = rank_listings_with_reasons([listing()], criteria)
        assert len(results) == 1
        assert any("Salary" in r for r in results[0].reasons)


# ---------------------------------------------------------------------------
# Integration tests with Django ORM
# ---------------------------------------------------------------------------


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class MatchingServiceIntegrationTests(TestCase):
    """Integration tests with real Django models."""

    def setUp(self):
        self.user = User.objects.create_user("matchuser", password="secret")
        self.org_public = Organization.objects.create(
            name="PublicCo", funding_round="P", rto_policy="R"
        )
        self.org_pre_ipo = Organization.objects.create(
            name="StartupCo", funding_round="A", rto_policy="O"
        )
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            enabled=True,
        )
        now = timezone.now()
        self.listing_public = JobListing.all_objects.create(
            source=self.source,
            external_id="pub-1",
            canonical_url="https://jobs.example.test/pub-1",
            employer_name="PublicCo",
            title="Senior Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=self.org_public,
            compensation_min=Decimal(150000),
            compensation_max=Decimal(180000),
            compensation_currency="USD",
            is_remote=True,
        )
        self.listing_pre_ipo = JobListing.all_objects.create(
            source=self.source,
            external_id="pre-2",
            canonical_url="https://jobs.example.test/pre-2",
            employer_name="StartupCo",
            title="Staff Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=self.org_pre_ipo,
            compensation_min=Decimal(130000),
            compensation_max=Decimal(160000),
            compensation_currency="USD",
            is_remote=False,
        )
        self.pref = UserPreference.objects.create(
            user=self.user,
            preferences=preferences(
                compensation={
                    "minimum_salary": 100000,
                    "currency": "USD",
                    "require_public_company": True,
                },
                work_location={"max_in_office_days": 0},
            ),
            schema_version=2,
        )

    def test_match_jobs_returns_only_matching_listings(self):
        """match_jobs should exclude pre-IPO and in-office when filters are set."""
        results = match_jobs(self.user, limit=10)
        listing_ids = [r.listing_id for r in results]
        assert self.listing_public.pk in listing_ids
        assert self.listing_pre_ipo.pk not in listing_ids

    def test_match_jobs_returns_reasons(self):
        results = match_jobs(self.user, limit=10)
        assert len(results) > 0
        pub_result = next(r for r in results if r.listing_id == self.listing_public.pk)
        assert "Public company" in pub_result.reasons
        assert "Remote" in pub_result.reasons

    def test_match_organizations_excludes_pre_ipo(self):
        results = match_organizations(self.user, limit=10)
        org_ids = [r.organization_id for r in results]
        assert self.org_public.pk in org_ids
        assert self.org_pre_ipo.pk not in org_ids

    def test_match_organizations_returns_reasons(self):
        results = match_organizations(self.user, limit=10)
        assert len(results) > 0
        pub_result = next(r for r in results if r.organization_id == self.org_public.pk)
        assert "Public company" in pub_result.reasons
        assert "Remote" in pub_result.reasons

    def test_match_jobs_returns_empty_when_no_preferences(self):
        user_no_prefs = User.objects.create_user("noprefs", password="secret")
        results = match_jobs(user_no_prefs, limit=10)
        assert results == []

    def test_match_organizations_returns_empty_when_no_preferences(self):
        user_no_prefs = User.objects.create_user("noprefs2", password="secret")
        results = match_organizations(user_no_prefs, limit=10)
        assert results == []

    def test_match_jobs_respects_limit(self):
        results = match_jobs(self.user, limit=1)
        assert len(results) <= 1

    def test_match_organizations_respects_limit(self):
        results = match_organizations(self.user, limit=1)
        assert len(results) <= 1


# ---------------------------------------------------------------------------
# Empty-inventory fallback tests
# ---------------------------------------------------------------------------


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class EmptyInventoryTests(TestCase):
    """Test that empty inventory returns empty results, not errors."""

    def setUp(self):
        self.user = User.objects.create_user("emptyuser", password="secret")
        UserPreference.objects.create(
            user=self.user,
            preferences=preferences(),
            schema_version=2,
        )

    def test_match_jobs_empty_inventory(self):
        results = match_jobs(self.user, limit=10)
        assert results == []

    def test_match_organizations_empty_inventory(self):
        results = match_organizations(self.user, limit=10)
        assert results == []


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class SchemaValidationTests(TestCase):
    """Test that new preference fields are validated correctly."""

    def test_max_in_office_days_range_validation(self):
        from crank.services.preferences import InvalidValueError, validate_value

        with self.assertRaises(InvalidValueError):
            validate_value("work_location.max_in_office_days", "int", 8)
        with self.assertRaises(InvalidValueError):
            validate_value("work_location.max_in_office_days", "int", -1)
        # Valid values
        assert validate_value("work_location.max_in_office_days", "int", 0) == 0
        assert validate_value("work_location.max_in_office_days", "int", 5) == 5
        assert validate_value("work_location.max_in_office_days", "int", None) is None

    def test_require_public_company_bool_validation(self):
        from crank.services.preferences import InvalidValueError, validate_value

        assert validate_value(
            "compensation.require_public_company", "bool", True
        ) is True
        assert validate_value(
            "compensation.require_public_company", "bool", False
        ) is False
        assert validate_value(
            "compensation.require_public_company", "bool", None
        ) is None
        with self.assertRaises(InvalidValueError):
            validate_value(
                "compensation.require_public_company", "bool", "yes"
            )

    def test_default_preferences_has_new_fields(self):
        doc = default_preferences()
        assert "require_public_company" in doc["compensation"]
        assert doc["compensation"]["require_public_company"] is None
        assert "max_in_office_days" in doc["work_location"]
        assert doc["work_location"]["max_in_office_days"] is None

    def test_schema_version_is_2(self):
        from crank.models.preference import SCHEMA_VERSION
        assert SCHEMA_VERSION == 2


# ---------------------------------------------------------------------------
# API contract tests
# ---------------------------------------------------------------------------


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class RankedAPIContractTests(TestCase):
    """Test the /api/job-matches/ranked/ endpoint contract."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("apiuser", password="secret")

    def test_anonymous_requests_are_rejected(self):
        response = self.client.get("/api/job-matches/ranked/")
        self.assertEqual(response.status_code, 302)

    def test_authenticated_returns_json_with_expected_keys(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/ranked/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("job_matches", payload)
        self.assertIn("organization_matches", payload)
        self.assertIsInstance(payload["job_matches"], list)
        self.assertIsInstance(payload["organization_matches"], list)

    def test_authenticated_no_preferences_returns_empty(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/ranked/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["job_matches"], [])
        self.assertEqual(payload["organization_matches"], [])


# ---------------------------------------------------------------------------
# Chat orchestrator tool tests
# ---------------------------------------------------------------------------


class ChatToolTests(TestCase):
    """Test the get_matches_for_user tool surface."""

    def test_get_matches_for_user_returns_bounded_results(self):
        from crank.agents.job_search.tools import get_matches_for_user

        user = SimpleNamespace(pk=1)
        fake_job = JobMatchResult(
            listing_id=1, title="Eng", employer_name="Co",
            organization_id=1, organization_name="Co",
            canonical_url="https://example.test/1",
            location_text="SF", is_remote=True, score=50.0,
            reasons=["Public company"],
        )
        fake_org = OrgMatchResult(
            organization_id=1, name="Co", url="https://example.test",
            funding_round="P", rto_policy="R", score=40.0,
            reasons=["Public company"],
        )

        def fake_service(user, limit=25):
            return [fake_job] * limit, [fake_org] * limit

        result = get_matches_for_user(user, limit=5, match_service=fake_service)
        assert len(result["job_matches"]) == 5
        assert len(result["organization_matches"]) == 5
        assert result["job_matches"][0]["listing_id"] == 1
        assert result["job_matches"][0]["reasons"] == ["Public company"]
        assert result["organization_matches"][0]["organization_id"] == 1

    def test_get_matches_for_user_clamps_limit(self):
        from crank.agents.job_search.tools import get_matches_for_user

        user = SimpleNamespace(pk=1)

        def fake_service(user, limit=25):
            return [], []

        result = get_matches_for_user(user, limit=1000, match_service=fake_service)
        # Should not raise; service clamps internally
        assert result["job_matches"] == []


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class HelperFunctionTests(TestCase):
    """Test internal helper functions for label and reason generation."""

    def test_funding_label_known(self):
        assert _funding_label("A") == "Series A"
        assert _funding_label("P") == "Public"

    def test_funding_label_unknown_code(self):
        assert _funding_label("Z") == "Z"
        assert _funding_label("") == "Unknown"
        assert _funding_label(None) == "Unknown"

    def test_rto_label_known(self):
        assert _rto_label("R") == "Remote"
        assert _rto_label("H") == "Hybrid"
        assert _rto_label("O") == "In-office"

    def test_rto_label_unknown_code(self):
        assert _rto_label("X") == "X"
        assert _rto_label("") == "Unknown"
        assert _rto_label(None) == "Unknown"

    def test_reasons_from_factors_public_company(self):
        criteria = project_criteria(preferences(), 2)
        public_org = org(funding_round="P")
        lst = listing(organization=public_org)
        from crank.agents.jobs.matching import FactorContribution
        factors = [FactorContribution("compensation", 0.5, 1.0, "min=100000")]
        reasons = _reasons_from_factors(factors, lst, public_org, criteria)
        assert "Public company" in reasons

    def test_reasons_from_factors_funding_label(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org(funding_round="A")
        lst = listing(organization=a_org)
        factors = []
        reasons = _reasons_from_factors(factors, lst, a_org, criteria)
        assert "Series A" in reasons

    def test_reasons_from_factors_in_office_rto(self):
        criteria = project_criteria(preferences(), 2)
        in_office_org = org(rto_policy="O")
        lst = listing(organization=in_office_org)
        factors = []
        reasons = _reasons_from_factors(factors, lst, in_office_org, criteria)
        assert "In-office" in reasons

    def test_reasons_from_factors_compensation(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        lst = listing(organization=a_org, compensation_min=Decimal(150000))
        from crank.agents.jobs.matching import FactorContribution
        factors = [FactorContribution("compensation", 0.8, 1.0, "min=100000")]
        reasons = _reasons_from_factors(factors, lst, a_org, criteria)
        assert any("Salary" in r for r in reasons)

    def test_reasons_from_factors_compensation_max_only(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        lst = listing(organization=a_org, compensation_min=None, compensation_max=Decimal(120000))
        from crank.agents.jobs.matching import FactorContribution
        factors = [FactorContribution("compensation", 0.8, 1.0, "min=100000")]
        reasons = _reasons_from_factors(factors, lst, a_org, criteria)
        assert any("Salary up to" in r for r in reasons)

    def test_reasons_from_factors_vesting(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        lst = listing(organization=a_org)
        from crank.agents.jobs.matching import FactorContribution
        factors = [FactorContribution("vesting", 0.9, 1.0, "good")]
        reasons = _reasons_from_factors(factors, lst, a_org, criteria)
        assert "Vesting aligns" in reasons

    def test_reasons_from_factors_culture(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        lst = listing(organization=a_org)
        from crank.agents.jobs.matching import FactorContribution
        factors = [FactorContribution("culture", 0.5, 1.0, "matched=transparent")]
        reasons = _reasons_from_factors(factors, lst, a_org, criteria)
        assert any("Culture" in r for r in reasons)

    def test_reasons_from_factors_industry(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        lst = listing(organization=a_org)
        from crank.agents.jobs.matching import FactorContribution
        factors = [FactorContribution("industry", 0.5, 1.0, "matched=software")]
        reasons = _reasons_from_factors(factors, lst, a_org, criteria)
        assert any("Industry" in r for r in reasons)

    def test_reasons_from_factors_org_score(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        lst = listing(organization=a_org)
        from crank.agents.jobs.matching import FactorContribution
        factors = [FactorContribution("organization_scores", 0.7, 1.0, "average=4.0/5.0")]
        reasons = _reasons_from_factors(factors, lst, a_org, criteria)
        assert any(r.startswith("Score ") for r in reasons)

    def test_reasons_from_factors_org_score_invalid(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        lst = listing(organization=a_org)
        from crank.agents.jobs.matching import FactorContribution
        factors = [FactorContribution("organization_scores", 0.7, 1.0, "average=abc/5.0")]
        reasons = _reasons_from_factors(factors, lst, a_org, criteria)
        assert not any(r.startswith("Score ") for r in reasons)

    def test_reasons_from_factors_culture_none(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        lst = listing(organization=a_org)
        from crank.agents.jobs.matching import FactorContribution
        factors = [FactorContribution("culture", 0.5, 1.0, "matched=none")]
        reasons = _reasons_from_factors(factors, lst, a_org, criteria)
        assert not any("Culture" in r for r in reasons)

    def test_reasons_from_factors_industry_none(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        lst = listing(organization=a_org)
        from crank.agents.jobs.matching import FactorContribution
        factors = [FactorContribution("industry", 0.5, 1.0, "matched=none")]
        reasons = _reasons_from_factors(factors, lst, a_org, criteria)
        assert not any("Industry" in r for r in reasons)

    def test_reasons_from_factors_recent_listing(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        lst = listing(organization=a_org)
        factors = []
        reasons = _reasons_from_factors(factors, lst, a_org, criteria)
        assert "Recent listing" in reasons

    def test_reasons_for_org_public_company(self):
        criteria = project_criteria(preferences(), 2)
        public_org = org(funding_round="P")
        reasons = _reasons_for_org(public_org, criteria, 50.0)
        assert "Public company" in reasons

    def test_reasons_for_org_funding_label(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org(funding_round="A")
        reasons = _reasons_for_org(a_org, criteria, 50.0)
        assert "Series A" in reasons

    def test_reasons_for_org_remote(self):
        criteria = project_criteria(preferences(), 2)
        remote_org = org(rto_policy="R")
        reasons = _reasons_for_org(remote_org, criteria, 50.0)
        assert "Remote" in reasons

    def test_reasons_for_org_hybrid(self):
        criteria = project_criteria(preferences(), 2)
        hybrid_org = org(rto_policy="H")
        reasons = _reasons_for_org(hybrid_org, criteria, 50.0)
        assert any("Hybrid" in r for r in reasons)

    def test_reasons_for_org_in_office(self):
        criteria = project_criteria(preferences(), 2)
        in_office_org = org(rto_policy="O")
        reasons = _reasons_for_org(in_office_org, criteria, 50.0)
        assert "In-office" in reasons

    def test_reasons_for_org_score(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        reasons = _reasons_for_org(a_org, criteria, 50.0)
        assert any(r.startswith("Score ") for r in reasons)

    def test_reasons_for_org_industry_match(self):
        criteria = project_criteria(
            preferences(industry=["software"]), 2,
        )
        a_org = org(industry="software")
        reasons = _reasons_for_org(a_org, criteria, 50.0)
        assert any("Industry" in r for r in reasons)

    def test_reasons_for_org_accelerated_vesting(self):
        criteria = project_criteria(
            preferences(vesting={"prefer_accelerated": True}), 2,
        )
        a_org = org(accelerated_vesting=True)
        reasons = _reasons_for_org(a_org, criteria, 50.0)
        assert "Accelerated vesting" in reasons

    def test_reasons_for_org_score_exception(self):
        criteria = project_criteria(preferences(), 2)
        bad_org = SimpleNamespace(
            name="Bad",
            funding_round="A",
            rto_policy=None,
            industry="",
            accelerated_vesting=False,
            avg_scores=lambda: (_ for _ in ()).throw(TypeError("bad")),
        )
        # Should not raise, should just skip score
        reasons = _reasons_for_org(bad_org, criteria, 50.0)
        assert not any(r.startswith("Score ") for r in reasons)

    def test_score_organization_match_funding(self):
        criteria = project_criteria(
            preferences(funding_stage=["A"]), 2,
        )
        a_org = org(funding_round="A")
        score = _score_organization_match(a_org, criteria, DEFAULT_CONFIG)
        assert score > 0

    def test_score_organization_match_rto(self):
        criteria = project_criteria(
            preferences(work_location={"modes": ["remote"]}), 2,
        )
        remote_org = org(rto_policy="R")
        score = _score_organization_match(remote_org, criteria, DEFAULT_CONFIG)
        assert score > 0

    def test_score_organization_match_industry(self):
        criteria = project_criteria(
            preferences(industry=["software"]), 2,
        )
        a_org = org(industry="software")
        score = _score_organization_match(a_org, criteria, DEFAULT_CONFIG)
        assert score > 0

    def test_score_organization_match_vesting(self):
        criteria = project_criteria(
            preferences(vesting={"prefer_accelerated": True}), 2,
        )
        a_org = org(accelerated_vesting=True)
        score = _score_organization_match(a_org, criteria, DEFAULT_CONFIG)
        assert score > 0

    def test_score_organization_match_org_scores(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        score = _score_organization_match(a_org, criteria, DEFAULT_CONFIG)
        assert score > 0

    def test_score_organization_match_zero(self):
        criteria = project_criteria(preferences(), 2)
        # Org with no matching attributes
        no_match_org = SimpleNamespace(
            funding_round=None,
            rto_policy=None,
            industry="",
            accelerated_vesting=None,
            avg_scores=list,
        )
        score = _score_organization_match(no_match_org, criteria, DEFAULT_CONFIG)
        assert score == 0.0

    def test_org_excluded_by_name(self):
        criteria = project_criteria(
            preferences(exclusions={"companies": ["bad co"]}), 2,
        )
        bad_org = org(name="Bad Co")
        assert _org_excluded(bad_org, criteria) is True

    def test_org_excluded_by_industry(self):
        criteria = project_criteria(
            preferences(exclusions={"industries": ["mining"]}), 2,
        )
        mining_org = org(industry="mining")
        assert _org_excluded(mining_org, criteria) is True

    def test_org_excluded_by_public_requirement(self):
        criteria = project_criteria(
            preferences(compensation={"require_public_company": True}), 2,
        )
        private_org = org(funding_round="A")
        assert _org_excluded(private_org, criteria) is True

    def test_org_excluded_by_rto_ceiling(self):
        criteria = project_criteria(
            preferences(work_location={"max_in_office_days": 2}), 2,
        )
        in_office_org = org(rto_policy="O")
        assert _org_excluded(in_office_org, criteria) is True

    def test_org_not_excluded_no_filters(self):
        criteria = project_criteria(preferences(), 2)
        a_org = org()
        assert _org_excluded(a_org, criteria) is False

    def test_org_not_excluded_rto_none(self):
        criteria = project_criteria(
            preferences(work_location={"max_in_office_days": 2}), 2,
        )
        # Org with no RTO policy set should not be excluded
        no_rto_org = org(rto_policy=None)
        assert _org_excluded(no_rto_org, criteria) is False

    def test_get_criteria_returns_none_for_no_user(self):
        from crank.models.preference import UserPreference
        user = SimpleNamespace(pk=999999)
        # Patch UserPreference.objects.get to raise DoesNotExist
        original_get = UserPreference.objects.get
        try:
            def raise_does_not_exist(**kwargs):
                raise UserPreference.DoesNotExist()
            UserPreference.objects.get = raise_does_not_exist
            result = _get_criteria(user)
            assert result is None
        finally:
            UserPreference.objects.get = original_get


class ReasonsFromStoredFactorsTests(TestCase):
    """Test the _reasons_from_stored_factors helper in views."""

    def test_none_factors(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        assert _reasons_from_stored_factors(None) == []

    def test_empty_list(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        assert _reasons_from_stored_factors([]) == []

    def test_non_list_factors(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        assert _reasons_from_stored_factors("not a list") == []

    def test_non_dict_factor(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        assert _reasons_from_stored_factors(["not a dict"]) == []

    def test_org_score_factor(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        factors = [{"factor": "organization_scores", "score": 0.8, "detail": "average=4.2/5.0"}]
        reasons = _reasons_from_stored_factors(factors)
        assert any("Score" in r for r in reasons)

    def test_org_score_factor_invalid_float(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        factors = [{"factor": "organization_scores", "score": 0.8, "detail": "average=abc/5.0"}]
        reasons = _reasons_from_stored_factors(factors)
        assert not any("Score" in r for r in reasons)

    def test_compensation_factor(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        factors = [{"factor": "compensation", "score": 0.7, "detail": ""}]
        reasons = _reasons_from_stored_factors(factors)
        assert "Compensation match" in reasons

    def test_work_location_factor(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        factors = [{"factor": "work_location", "score": 0.7, "detail": ""}]
        reasons = _reasons_from_stored_factors(factors)
        assert "Location match" in reasons

    def test_vesting_factor(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        factors = [{"factor": "vesting", "score": 0.7, "detail": ""}]
        reasons = _reasons_from_stored_factors(factors)
        assert "Vesting aligns" in reasons

    def test_culture_factor(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        factors = [{"factor": "culture", "score": 0.5, "detail": "matched=transparent"}]
        reasons = _reasons_from_stored_factors(factors)
        assert any("Culture" in r for r in reasons)

    def test_culture_factor_none(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        factors = [{"factor": "culture", "score": 0.5, "detail": "matched=none"}]
        reasons = _reasons_from_stored_factors(factors)
        assert not any("Culture" in r for r in reasons)

    def test_industry_factor(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        factors = [{"factor": "industry", "score": 0.5, "detail": "matched=software"}]
        reasons = _reasons_from_stored_factors(factors)
        assert any("Industry" in r for r in reasons)

    def test_industry_factor_none(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        factors = [{"factor": "industry", "score": 0.5, "detail": "matched=none"}]
        reasons = _reasons_from_stored_factors(factors)
        assert not any("Industry" in r for r in reasons)

    def test_reasons_bounded_to_six(self):
        from crank.views.job_matches import _reasons_from_stored_factors
        factors = [
            {"factor": "compensation", "score": 0.7, "detail": ""},
            {"factor": "work_location", "score": 0.7, "detail": ""},
            {"factor": "vesting", "score": 0.7, "detail": ""},
            {"factor": "culture", "score": 0.5, "detail": "matched=transparent"},
            {"factor": "industry", "score": 0.5, "detail": "matched=software"},
            {"factor": "organization_scores", "score": 0.8, "detail": "average=4.2/5.0"},
            {"factor": "extra", "score": 0.5, "detail": ""},
        ]
        reasons = _reasons_from_stored_factors(factors)
        assert len(reasons) <= 6


# ---------------------------------------------------------------------------
# Ranked API contract tests with matches
# ---------------------------------------------------------------------------


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class RankedAPIWithMatchesTests(TestCase):
    """Test the ranked endpoint with actual match data."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("rankuser", password="secret")
        self.org = Organization.objects.create(
            name="RankedCo", funding_round="P", rto_policy="R",
        )
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic", adapter_key="synthetic.v1",
            base_url="https://jobs.example.test", enabled=True,
        )
        now = timezone.now()
        self.job = JobListing.all_objects.create(
            source=self.source,
            external_id="rank-1",
            canonical_url="https://jobs.example.test/rank-1",
            employer_name="RankedCo",
            title="Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=self.org,
            compensation_min=Decimal(150000),
            compensation_max=Decimal(180000),
            compensation_currency="USD",
            is_remote=True,
        )
        self.pref = UserPreference.objects.create(
            user=self.user,
            preferences=preferences(
                compensation={"minimum_salary": 100000, "currency": "USD"},
            ),
            schema_version=2,
        )

    def test_ranked_returns_matches(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/ranked/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        assert len(payload["job_matches"]) > 0
        assert len(payload["organization_matches"]) > 0
        job = payload["job_matches"][0]
        assert "listing_id" in job
        assert "title" in job
        assert "score" in job
        assert "reasons" in job
        assert "factors" in job

    def test_ranked_with_limit(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/ranked/?limit=1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        assert len(payload["job_matches"]) <= 1

    def test_ranked_invalid_limit(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/job-matches/ranked/?limit=abc")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        # Should fall back to default limit
        assert "job_matches" in payload
