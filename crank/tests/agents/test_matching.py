# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for deterministic preference projection and job matching."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from crank.agents.jobs.matching import (
    ExclusionReason,
    project_criteria,
    rank_listing,
    rank_listings,
)
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG, RankingConfig, config_for_version


FACTORS = (
    "work_location",
    "geography",
    "compensation",
    "industry",
    "funding_stage",
    "culture",
    "vesting",
    "organization_scores",
)


def preferences(**overrides):
    result = {
        "compensation": {"minimum_salary": None, "currency": "USD", "equity_minimum_percent": None},
        "culture": [],
        "work_location": {"modes": [], "countries": [], "require_onsite": None},
        "geography": {"regions": [], "remote_friendly": None},
        "industry": [],
        "funding_stage": [],
        "vesting": {"max_cliff_months": None, "max_vesting_months": None, "prefer_accelerated": None},
        "exclusions": {"companies": [], "titles": [], "industries": [], "locations": []},
        "priorities": {},
    }
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
        "compensation_min": Decimal("120000"),
        "compensation_max": Decimal("140000"),
        "compensation_currency": "USD",
        "compensation_interval": "year",
        "description_excerpt": "Remote-first, collaborative team",
        "status": "active",
        "source_metadata": {},
    }
    if organization is None:
        defaults["organization"] = None
    elif organization == "__default__":
        defaults["organization"] = org()
    else:
        defaults["organization"] = organization
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_projection_normalizes_all_sections_and_versions():
    criteria = project_criteria(
        preferences(
            compensation={"minimum_salary": "100000", "currency": "usd", "equity_minimum_percent": 1.5},
            culture=["Remote-first"],
            work_location={"modes": ["onsite", "REMOTE"], "countries": ["US"]},
            geography={"regions": ["Pacific Northwest"], "remote_friendly": True},
            industry=["Software"],
            funding_stage=["Series A"],
            vesting={"max_cliff_months": "12", "max_vesting_months": 48, "prefer_accelerated": True},
            exclusions={"companies": [" Bad Co "], "titles": ["intern"], "industries": ["crypto"], "locations": ["Texas"]},
            priorities={"compensation": 0.8},
        ),
        7,
    )
    assert criteria.criteria_version == 7
    assert criteria.currency == "USD"
    assert criteria.min_salary == Decimal("100000")
    assert criteria.work_modes == frozenset({"in-office", "remote"})
    assert criteria.funding_stages == frozenset({"series a"})
    assert criteria.excluded_companies == frozenset({"bad co"})
    assert criteria.priorities == {"compensation": 0.8}


@pytest.mark.parametrize("field, value", [("employer_name", "Bad Co"), ("title", "Intern - Platform"), ("location_text", "Texas")])
def test_hard_exclusions_have_reason_codes(field, value):
    exclusion_key = {"employer_name": "companies", "title": "titles", "location_text": "locations"}[field]
    criteria = project_criteria(preferences(exclusions={exclusion_key: [value]}), 1)
    result = rank_listing(listing(**{field: value}), criteria)
    assert result.excluded is True
    assert result.score == 0
    assert result.factors == []
    assert result.exclusion_reasons == [getattr(ExclusionReason, {"employer_name": "EXCLUDED_COMPANY", "title": "EXCLUDED_TITLE", "location_text": "EXCLUDED_LOCATION"}[field]).value]


def test_industry_inactive_and_missing_organization_exclusions():
    criteria = project_criteria(preferences(exclusions={"industries": ["crypto"]}), 1)
    industry_result = rank_listing(listing(organization=org(industry="Crypto")), criteria)
    assert industry_result.exclusion_reasons == [ExclusionReason.EXCLUDED_INDUSTRY.value]
    closed = rank_listing(listing(status="closed"), project_criteria(preferences(), 1))
    assert ExclusionReason.INACTIVE_LISTING.value in closed.exclusion_reasons
    missing = rank_listing(listing(organization=None), project_criteria(preferences(), 1))
    assert missing.exclusion_reasons == [ExclusionReason.NO_ORGANIZATION.value]


def test_each_factor_is_present_and_deterministically_explainable():
    criteria = project_criteria(
        preferences(
            compensation={"minimum_salary": 100000, "currency": "USD"},
            work_location={"modes": ["remote"], "countries": ["United States"]},
            geography={"regions": ["United"], "remote_friendly": True},
            industry=["software"], funding_stage=["Series A"], culture=["remote-first"],
            vesting={"prefer_accelerated": True},
        ), 1,
    )
    result = rank_listing(listing(), criteria)
    assert [factor.factor for factor in result.factors] == list(FACTORS)
    assert result.score == sum(factor.score for factor in result.factors)
    assert all(0 <= factor.score <= factor.max_score for factor in result.factors)
    assert all(factor.detail for factor in result.factors)
    assert result.ranker_version == DEFAULT_CONFIG.version


def test_missing_fields_are_neutral_and_unknown_scores_are_bounded():
    criteria = project_criteria(
        preferences(compensation={"minimum_salary": 100000}, work_location={"modes": ["remote"]}, industry=["software"]), 1,
    )
    result = rank_listing(
        listing(location_text="", compensation_min=None, compensation_max=None, compensation_currency="", description_excerpt="", organization=org(industry=None, avg_scores=lambda: [])),
        criteria,
    )
    assert not result.excluded
    assert result.score > 0
    assert all(0 <= factor.score <= factor.max_score for factor in result.factors)


def test_weight_priority_and_custom_max_score_are_respected():
    criteria = project_criteria(preferences(work_location={"modes": ["remote"]}, priorities={"work_location": 2.0}), 1)
    config = RankingConfig(version="2.0.0", weights={name: 0.0 for name in FACTORS} | {"work_location": 1.0}, max_score=40)
    result = rank_listing(listing(), criteria, config)
    assert result.score == 40
    assert result.factors[0].max_score == 80
    assert result.ranker_version == "2.0.0"


def test_compensation_boundaries_currency_and_equity():
    criteria = project_criteria(preferences(compensation={"minimum_salary": 100000, "currency": "USD", "equity_minimum_percent": 2}), 1)
    meets = rank_listing(listing(source_metadata={"equity_percent": 2}), criteria)
    below = rank_listing(listing(compensation_max=Decimal("99999"), source_metadata={"equity_percent": 2}), criteria)
    mismatch = rank_listing(listing(compensation_currency="EUR"), criteria)
    assert next(f for f in meets.factors if f.factor == "compensation").score > next(f for f in below.factors if f.factor == "compensation").score
    assert next(f for f in mismatch.factors if f.factor == "compensation").score == 0


def test_vesting_limits_and_organization_score_shape():
    criteria = project_criteria(preferences(vesting={"max_cliff_months": 12, "max_vesting_months": 48, "prefer_accelerated": True}), 1)
    target = rank_listing(listing(organization=org(cliff_months=12, vesting_months=48)), criteria)
    assert next(f for f in target.factors if f.factor == "vesting").score > 0
    no_scores = rank_listing(listing(organization=org(avg_scores=lambda: [{"avg_score": "bad"}])), criteria)
    assert next(f for f in no_scores.factors if f.factor == "organization_scores").detail.endswith("unknown")


def test_ties_are_sorted_by_listing_id_and_repeated_runs_match():
    criteria = project_criteria(preferences(), 3)
    rows = [listing(pk=20), listing(pk=3)]
    first = rank_listings(rows, criteria)
    second = rank_listings(list(reversed(rows)), criteria)
    assert [result.listing_id for result in first] == [3, 20]
    assert first == second


def test_config_registry_and_invalid_config_values():
    assert config_for_version(DEFAULT_CONFIG.version) == DEFAULT_CONFIG
    with pytest.raises(ValueError):
        config_for_version("9.9.9")
    with pytest.raises(ValueError):
        RankingConfig(version="x", weights={"unknown": 1})
    with pytest.raises(ValueError):
        RankingConfig(version="x", weights={"work_location": -1})


# ---------------------------------------------------------------------------
# Coverage gap tests
# ---------------------------------------------------------------------------


def test_text_and_normalized_helpers():
    """Cover _text None path (line 28) and _normalized edge cases."""
    from crank.agents.jobs.matching import _text, _normalized, _values, _strings

    assert _text(None) == ""
    assert _text("  Hello  World  ") == "hello world"
    assert _normalized(None) == ""
    assert _normalized("Hello!! World") == "hello world"


def test_values_helper_edge_cases():
    """Cover _values Mapping branch (line 57) and TypeError fallback (lines 60-61)."""
    from crank.agents.jobs.matching import _values

    # Mapping input returns keys
    assert _values({"a": 1, "b": 2}) == ("a", "b")
    # Non-iterable scalar
    assert _values(42) == (42,)


def test_metadata_from_organization():
    """Cover _metadata merging from org source_metadata (line 212)."""
    from crank.agents.jobs.matching import _metadata, _organization_value

    listing = SimpleNamespace(source_metadata={"equity_percent": 1.5})
    organization = SimpleNamespace(source_metadata={"culture": "remote-first"})
    result = _metadata(listing, organization)
    assert result["equity_percent"] == 1.5
    assert result["culture"] == "remote-first"

    # Cover line 212: attribute not on org object but present in metadata
    org_with_meta = SimpleNamespace(source_metadata={"industry": "Biotech"}, industry=None)
    value = _organization_value(listing, org_with_meta, "industry")
    assert value == "Biotech"


def test_location_mode_non_remote_with_org():
    """Cover _location_mode is_remote=False with org (lines 236-245)."""
    from crank.agents.jobs.matching import _location_mode

    # is_remote=False, org has rto_policy="R" -> "remote"
    listing_obj = SimpleNamespace(is_remote=False)
    organization = SimpleNamespace(rto_policy="R", source_metadata={})
    assert _location_mode(listing_obj, organization) == "remote"

    # is_remote=False, org has empty rto_policy -> "in-office" (line 241)
    organization2 = SimpleNamespace(rto_policy="", source_metadata={})
    assert _location_mode(listing_obj, organization2) == "in-office"

    # is_remote=False, org has rto_policy="O" -> "in-office"
    organization3 = SimpleNamespace(rto_policy="O", source_metadata={})
    assert _location_mode(listing_obj, organization3) == "in-office"

    # is_remote=None, falls through to policy lookup
    listing_none = SimpleNamespace(is_remote=None)
    assert _location_mode(listing_none, organization) == "remote"


def test_work_location_missing_mode():
    """Cover _score_work_location missing mode (line 253)."""
    criteria = project_criteria(preferences(work_location={"modes": ["remote"]}), 1)
    # listing with is_remote=None, org with no rto_policy
    result = rank_listing(
        listing(is_remote=None, organization=org(rto_policy="", source_metadata={})),
        criteria,
    )
    factor = next(f for f in result.factors if f.factor == "work_location")
    assert "unknown" in factor.detail


def test_geography_missing_location():
    """Cover _score_geography unknown location (line 269)."""
    criteria = project_criteria(
        preferences(work_location={"modes": ["remote"]}, geography={"regions": ["California"]}),
        1,
    )
    result = rank_listing(listing(location_text="", is_remote=None), criteria)
    factor = next(f for f in result.factors if f.factor == "geography")
    assert "unknown" in factor.detail


def test_compensation_partial_range():
    """Cover value=0.5 partial range (line 293) and equity unknown (line 298)."""
    criteria = project_criteria(
        preferences(compensation={"minimum_salary": 100000, "currency": "USD", "equity_minimum_percent": 2}),
        1,
    )
    # min=None, max=120000 -> partial match (min unknown but max >= threshold)
    result = rank_listing(
        listing(compensation_min=None, compensation_max=Decimal("120000")),
        criteria,
    )
    factor = next(f for f in result.factors if f.factor == "compensation")
    assert "equity unknown" in factor.detail


def test_culture_missing():
    """Cover _score_culture no haystack (lines 328-329)."""
    criteria = project_criteria(preferences(culture=["innovation"]), 1)
    # Both description_excerpt and org culture/tags must be None/empty
    result = rank_listing(
        listing(description_excerpt=None, organization=org(source_metadata={}, culture_tags=None, culture=None)),
        criteria,
    )
    factor = next(f for f in result.factors if f.factor == "culture")
    assert "unknown" in factor.detail


def test_vesting_no_data():
    """Cover _score_vesting vesting data unknown (line 365)."""
    criteria = project_criteria(preferences(vesting={"prefer_accelerated": True}), 1)
    result = rank_listing(
        listing(organization=org(accelerated_vesting=None, source_metadata={})),
        criteria,
    )
    factor = next(f for f in result.factors if f.factor == "vesting")
    assert "unknown" in factor.detail


def test_organization_scores_error_handling():
    """Cover avg_scores exception (lines 372-373) and non-Mapping rows (line 377)."""
    criteria = project_criteria(preferences(), 1)
    # avg_scores raises TypeError
    result = rank_listing(
        listing(organization=org(avg_scores=lambda: (_ for _ in ()).throw(TypeError("bad")))),
        criteria,
    )
    factor = next(f for f in result.factors if f.factor == "organization_scores")
    assert "unknown" in factor.detail

    # avg_scores returns non-Mapping items
    result2 = rank_listing(
        listing(organization=org(avg_scores=lambda: [42, "bad"])),
        criteria,
    )
    factor2 = next(f for f in result2.factors if f.factor == "organization_scores")
    assert "unknown" in factor2.detail


def test_ranking_config_validation():
    """Cover RankingConfig validation errors (lines 41, 43, 45)."""
    with pytest.raises(ValueError, match="version must be a non-empty string"):
        RankingConfig(version="", weights={"work_location": 1})
    with pytest.raises(ValueError, match="max_score must be positive"):
        RankingConfig(version="x", weights={"work_location": 1}, max_score=-1)
    with pytest.raises(ValueError, match="missing_data_penalty must be finite"):
        RankingConfig(version="x", weights={"work_location": 1}, missing_data_penalty=float("nan"))
