# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Deterministic preference projection and job-listing ranking."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Any, Iterable, Mapping

from crank.agents.jobs.ranking_config import DEFAULT_CONFIG, RankingConfig


_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s-]+", re.UNICODE)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return _WS.sub(" ", str(value).strip()).casefold()


def _normalized(value: Any) -> str:
    if value is None:
        return ""
    return _text(_NON_WORD.sub(" ", str(value)))


def _canonical_stage(value: Any) -> str:
    """Normalize the several labels used for the same funding stage."""

    stage = _normalized(value).replace("_", " ")
    aliases = {
        "series a": "a",
        "series b": "b",
        "series c": "c",
        "series d": "d",
        "series e": "e",
        "series f": "f",
        "series g or later": "x",
        "series x": "x",
        "other private": "o",
        "public": "p",
    }
    return aliases.get(stage, stage)


def _values(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (value,) if str(value).strip() else ()
    if isinstance(value, Mapping):
        return tuple(value.keys())
    try:
        return tuple(value)
    except TypeError:
        return (value,)


def _strings(value: Any) -> frozenset[str]:
    return frozenset(item for item in (_normalized(v) for v in _values(value)) if item)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _integer(value: Any) -> int | None:
    number = _decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class JobCriteria:
    """Immutable projection of the canonical preference document."""

    excluded_companies: frozenset[str] = frozenset()
    excluded_titles: frozenset[str] = frozenset()
    excluded_industries: frozenset[str] = frozenset()
    excluded_locations: frozenset[str] = frozenset()
    min_salary: Decimal | None = None
    currency: str = "USD"
    equity_minimum: float | None = None
    work_modes: frozenset[str] = frozenset()
    countries: frozenset[str] = frozenset()
    regions: frozenset[str] = frozenset()
    remote_friendly: bool | None = None
    industries: frozenset[str] = frozenset()
    funding_stages: frozenset[str] = frozenset()
    max_cliff_months: int | None = None
    max_vesting_months: int | None = None
    prefer_accelerated: bool | None = None
    culture_tags: frozenset[str] = frozenset()
    priorities: dict[str, float] = field(default_factory=dict)
    criteria_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "priorities", dict(sorted(self.priorities.items())))


def project_criteria(prefs: dict, schema_version: int) -> JobCriteria:
    """Project a versioned raw preference document into matching criteria.

    Unknown or malformed optional values are omitted rather than becoming a
    scoring contradiction.  This preserves the canonical document's forward
    compatibility and gives missing listing fields neutral treatment.
    """

    prefs = prefs if isinstance(prefs, Mapping) else {}
    compensation = prefs.get("compensation") or {}
    work_location = prefs.get("work_location") or {}
    geography = prefs.get("geography") or {}
    vesting = prefs.get("vesting") or {}
    exclusions = prefs.get("exclusions") or {}
    priorities = prefs.get("priorities") or {}
    modes = {_normalized(v) for v in _values(work_location.get("modes")) if _normalized(v)}
    aliases = {"onsite": "in-office", "office": "in-office", "in_office": "in-office"}
    modes = frozenset(aliases.get(mode, mode) for mode in modes)
    raw_priorities: dict[str, float] = {}
    if isinstance(priorities, Mapping):
        for key, value in priorities.items():
            number = _decimal(value)
            if number is not None:
                raw_priorities[_normalized(key)] = float(number)
    equity = _decimal(compensation.get("equity_minimum_percent"))
    return JobCriteria(
        excluded_companies=_strings(exclusions.get("companies")),
        excluded_titles=_strings(exclusions.get("titles")),
        excluded_industries=_strings(exclusions.get("industries")),
        excluded_locations=_strings(exclusions.get("locations")),
        min_salary=_decimal(compensation.get("minimum_salary")),
        currency=(_text(compensation.get("currency")) or "usd").upper(),
        equity_minimum=float(equity) if equity is not None else None,
        work_modes=modes,
        countries=_strings(work_location.get("countries")),
        regions=_strings(geography.get("regions")),
        remote_friendly=_boolean(geography.get("remote_friendly")),
        industries=_strings(prefs.get("industry")),
        funding_stages=_strings(prefs.get("funding_stage")),
        max_cliff_months=_integer(vesting.get("max_cliff_months")),
        max_vesting_months=_integer(vesting.get("max_vesting_months")),
        prefer_accelerated=_boolean(vesting.get("prefer_accelerated")),
        culture_tags=_strings(prefs.get("culture")),
        priorities=raw_priorities,
        criteria_version=int(schema_version),
    )


class ExclusionReason(str, Enum):
    EXCLUDED_COMPANY = "excluded_company"
    EXCLUDED_TITLE = "excluded_title"
    EXCLUDED_INDUSTRY = "excluded_industry"
    EXCLUDED_LOCATION = "excluded_location"
    INACTIVE_LISTING = "inactive_listing"
    NO_ORGANIZATION = "no_organization"


@dataclass(frozen=True)
class FactorContribution:
    factor: str
    score: float
    max_score: float
    detail: str


@dataclass(frozen=True)
class MatchResult:
    listing_id: int
    score: float
    excluded: bool
    exclusion_reasons: list[str]
    factors: list[FactorContribution]
    ranker_version: str
    criteria_version: int


def _contains(text: str, candidates: Iterable[str]) -> bool:
    return any(candidate and candidate in text for candidate in candidates)


def _metadata(listing: Any, organization: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source in (getattr(listing, "source_metadata", None), getattr(organization, "source_metadata", None)):
        if isinstance(source, Mapping):
            result.update(source)
    return result


def _organization_value(listing: Any, organization: Any, *names: str) -> Any:
    metadata = _metadata(listing, organization)
    for name in names:
        value = getattr(organization, name, None)
        if value not in (None, ""):
            return value
        if name in metadata and metadata[name] not in (None, ""):
            return metadata[name]
    return None


def _missing_value(config: RankingConfig) -> float:
    return max(0.0, min(1.0, 0.5 - config.missing_data_penalty))


def _priority(criteria: JobCriteria, factor: str) -> float:
    aliases = {"organization": "organization_scores", "org_scores": "organization_scores", "salary": "compensation"}
    value = criteria.priorities.get(factor, criteria.priorities.get(aliases.get(factor, ""), 1.0))
    return max(0.0, float(value))


def _factor(factor: str, normalized_score: float, detail: str, criteria: JobCriteria, config: RankingConfig) -> FactorContribution:
    weight = float(config.weights.get(factor, 0.0)) * _priority(criteria, factor)
    maximum = float(config.max_score) * weight
    score = max(0.0, min(maximum, float(normalized_score) * maximum))
    return FactorContribution(factor, round(score, 10), round(maximum, 10), detail)


def _location_mode(listing: Any, organization: Any) -> str | None:
    if getattr(listing, "is_remote", None) is True:
        return "remote"
    if getattr(listing, "is_remote", None) is False and organization is not None:
        # JobListing has a concrete boolean (False means a non-remote listing),
        # while an organization's RTO policy can refine that to hybrid.
        policy = _organization_value(listing, organization, "rto_policy")
        if policy in (None, ""):
            return "in-office"
    policy = _organization_value(listing, organization, "rto_policy")
    normalized = _normalized(policy)
    mapping = {"r": "remote", "remote": "remote", "h": "hybrid", "hybrid": "hybrid", "o": "in-office", "in-office": "in-office", "onsite": "in-office"}
    return mapping.get(normalized)


def _score_work_location(listing: Any, criteria: JobCriteria, config: RankingConfig, organization: Any) -> FactorContribution:
    if not criteria.work_modes:
        return _factor("work_location", _missing_value(config), "no work-location preference", criteria, config)
    mode = _location_mode(listing, organization)
    if mode is None:
        return _factor("work_location", _missing_value(config), "listing work mode unknown", criteria, config)
    value = 1.0 if mode in criteria.work_modes else 0.0
    return _factor("work_location", value, f"listing mode {mode}; preferred={','.join(sorted(criteria.work_modes))}", criteria, config)


def _score_geography(listing: Any, criteria: JobCriteria, config: RankingConfig) -> FactorContribution:
    location_preferences = (*criteria.countries, *criteria.regions)
    remote = getattr(listing, "is_remote", None)
    if not location_preferences and criteria.remote_friendly is None:
        return _factor("geography", _missing_value(config), "no geography preference", criteria, config)
    location = _normalized(getattr(listing, "location_text", ""))
    matches = [item for item in location_preferences if item in location] if location else []
    values = [1.0 if matches else 0.0] if location_preferences and location else []
    if criteria.remote_friendly is not None and isinstance(remote, bool):
        values.append(1.0 if remote == criteria.remote_friendly else 0.0)
    if not values:
        return _factor("geography", _missing_value(config), "listing geography unknown", criteria, config)
    value = max(values)
    detail = "matched=" + (",".join(sorted(matches)) if matches else "none")
    if criteria.remote_friendly is not None:
        detail += f"; remote={remote}"
    return _factor("geography", value, detail, criteria, config)


def _score_compensation(listing: Any, criteria: JobCriteria, config: RankingConfig) -> FactorContribution:
    if criteria.min_salary is None:
        return _factor("compensation", _missing_value(config), "no minimum salary preference", criteria, config)
    minimum = _decimal(getattr(listing, "compensation_min", None))
    maximum = _decimal(getattr(listing, "compensation_max", None))
    if minimum is None and maximum is None:
        return _factor("compensation", _missing_value(config), "listing compensation unknown", criteria, config)
    currency = _text(getattr(listing, "compensation_currency", "")).upper()
    if currency and currency != criteria.currency:
        return _factor("compensation", 0.0, f"currency mismatch: {currency} != {criteria.currency}", criteria, config)
    threshold = criteria.min_salary
    if maximum is not None and maximum < threshold:
        value = 0.0
    elif minimum is not None and minimum >= threshold:
        value = 1.0
    else:
        value = 0.5
    detail = f"range={minimum or '?'}-{maximum or '?'}; minimum={threshold}"
    if criteria.equity_minimum is not None:
        equity = _decimal(_metadata(listing, getattr(listing, "organization", None)).get("equity_percent"))
        if equity is None:
            detail += "; equity unknown"
        else:
            equity_value = 1.0 if equity >= Decimal(str(criteria.equity_minimum)) else 0.0
            value = (value + equity_value) / 2.0
            detail += f"; equity={equity}%/{criteria.equity_minimum}%"
    return _factor("compensation", value, detail, criteria, config)


def _score_set_factor(factor: str, listing: Any, criteria_values: frozenset[str], config: RankingConfig, criteria: JobCriteria, organization: Any, *names: str) -> FactorContribution:
    if not criteria_values:
        return _factor(factor, _missing_value(config), f"no {factor} preference", criteria, config)
    value = _organization_value(listing, organization, *names)
    actual = _strings(value)
    if factor == "funding_stage":
        actual = frozenset(_canonical_stage(item) for item in actual)
        criteria_values = frozenset(_canonical_stage(item) for item in criteria_values)
    if not actual:
        return _factor(factor, _missing_value(config), f"listing {factor} unknown", criteria, config)
    matches = actual & criteria_values
    return _factor(factor, 1.0 if matches else 0.0, "matched=" + (",".join(sorted(matches)) if matches else "none"), criteria, config)


def _score_culture(listing: Any, criteria: JobCriteria, config: RankingConfig, organization: Any) -> FactorContribution:
    if not criteria.culture_tags:
        return _factor("culture", _missing_value(config), "no culture preference", criteria, config)
    metadata = _metadata(listing, organization)
    tags = _strings(_organization_value(listing, organization, "culture_tags", "culture"))
    haystack = " ".join((_text(getattr(listing, "description_excerpt", "")), _text(metadata.get("culture"))))
    matches = {tag for tag in criteria.culture_tags if tag in haystack or tag in tags}
    if not haystack.strip() and not tags:
        value = _missing_value(config)
        detail = "listing culture unknown"
    else:
        value = 1.0 if matches else 0.0
        detail = "matched=" + (",".join(sorted(matches)) if matches else "none")
    return _factor("culture", value, detail, criteria, config)


def _score_vesting(listing: Any, criteria: JobCriteria, config: RankingConfig, organization: Any) -> FactorContribution:
    has_preference = any(
        value is not None
        for value in (
            criteria.prefer_accelerated,
            criteria.max_cliff_months,
            criteria.max_vesting_months,
        )
    )
    if not has_preference:
        return _factor("vesting", _missing_value(config), "no vesting preference", criteria, config)
    checks: list[float] = []
    details: list[str] = []
    if criteria.prefer_accelerated is not None:
        accelerated = _organization_value(listing, organization, "accelerated_vesting")
        if isinstance(accelerated, bool):
            checks.append(1.0 if accelerated == criteria.prefer_accelerated else 0.0)
            details.append(f"accelerated={accelerated}")
    for field, maximum in (
        ("cliff_months", criteria.max_cliff_months),
        ("vesting_months", criteria.max_vesting_months),
    ):
        if maximum is None:
            continue
        actual = _decimal(_organization_value(listing, organization, field, f"max_{field}"))
        if actual is not None:
            checks.append(1.0 if actual <= maximum else 0.0)
            details.append(f"{field}={actual}/{maximum}")
    if not checks:
        return _factor("vesting", _missing_value(config), "vesting data unknown", criteria, config)
    return _factor("vesting", sum(checks) / len(checks), "; ".join(details), criteria, config)


def _score_organization(listing: Any, criteria: JobCriteria, config: RankingConfig, organization: Any) -> FactorContribution:
    try:
        scores = organization.avg_scores()
    except (AttributeError, TypeError, ValueError):
        scores = None
    values: list[float] = []
    for row in scores or ():
        if not isinstance(row, Mapping):
            continue
        value = _decimal(row.get("avg_score", row.get("score")))
        if value is not None:
            values.append(float(value))
    if not values:
        return _factor("organization_scores", _missing_value(config), "organization scores unknown", criteria, config)
    average = max(0.0, min(5.0, sum(values) / len(values)))
    return _factor("organization_scores", average / 5.0, f"average={average:.4g}/5", criteria, config)


def _excluded(listing: Any, criteria: JobCriteria) -> list[ExclusionReason]:
    reasons: list[ExclusionReason] = []
    company = _normalized(getattr(listing, "employer_name", ""))
    title = _normalized(getattr(listing, "title", ""))
    location = _normalized(getattr(listing, "location_text", ""))
    organization = getattr(listing, "organization", None)
    industry = _strings(_organization_value(listing, organization, "industry", "industries"))
    if company and _contains(company, criteria.excluded_companies):
        reasons.append(ExclusionReason.EXCLUDED_COMPANY)
    if title and _contains(title, criteria.excluded_titles):
        reasons.append(ExclusionReason.EXCLUDED_TITLE)
    if industry and industry & criteria.excluded_industries:
        reasons.append(ExclusionReason.EXCLUDED_INDUSTRY)
    if location and _contains(location, criteria.excluded_locations):
        reasons.append(ExclusionReason.EXCLUDED_LOCATION)
    if getattr(listing, "status", None) in {"closed", "expired"}:
        reasons.append(ExclusionReason.INACTIVE_LISTING)
    if organization is None:
        reasons.append(ExclusionReason.NO_ORGANIZATION)
    return reasons


def rank_listing(listing: Any, criteria: JobCriteria, config: RankingConfig = DEFAULT_CONFIG) -> MatchResult:
    """Apply hard exclusions, then calculate deterministic factor contributions."""

    reasons = _excluded(listing, criteria)
    listing_id = int(getattr(listing, "pk", getattr(listing, "id", 0)) or 0)
    if reasons:
        return MatchResult(listing_id, 0.0, True, [reason.value for reason in reasons], [], config.version, criteria.criteria_version)
    organization = listing.organization
    factors = [
        _score_work_location(listing, criteria, config, organization),
        _score_geography(listing, criteria, config),
        _score_compensation(listing, criteria, config),
        _score_set_factor("industry", listing, criteria.industries, config, criteria, organization, "industry", "industries"),
        _score_set_factor("funding_stage", listing, criteria.funding_stages, config, criteria, organization, "funding_round", "funding_stage"),
        _score_culture(listing, criteria, config, organization),
        _score_vesting(listing, criteria, config, organization),
        _score_organization(listing, criteria, config, organization),
    ]
    score = max(0.0, min(float(config.max_score), sum(factor.score for factor in factors)))
    return MatchResult(listing_id, round(score, 10), False, [], factors, config.version, criteria.criteria_version)


def rank_listings(listings: Iterable[Any], criteria: JobCriteria, config: RankingConfig = DEFAULT_CONFIG) -> list[MatchResult]:
    """Rank listings by descending score and ascending ID for ties."""

    results = [rank_listing(listing, criteria, config) for listing in listings]
    return sorted(results, key=lambda result: (-result.score, result.listing_id))


__all__ = [
    "ExclusionReason", "FactorContribution", "JobCriteria", "MatchResult",
    "project_criteria", "rank_listing", "rank_listings",
]
