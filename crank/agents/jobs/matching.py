# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Deterministic preference projection and job-listing ranking."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from functools import lru_cache
from typing import Any

from crank.agents.jobs.ranking_config import DEFAULT_CONFIG, RankingConfig

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s-]+", re.UNICODE)


@lru_cache(maxsize=8192)
def _text_cached(value: str) -> str:
    return _WS.sub(" ", value.strip()).casefold()


@lru_cache(maxsize=8192)
def _normalized_cached(value: str) -> str:
    return _text_cached(_NON_WORD.sub(" ", value))


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _text_cached(value)
    return _WS.sub(" ", str(value).strip()).casefold()


def _normalized(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _normalized_cached(value)
    return _text(_NON_WORD.sub(" ", str(value)))


def _canonical_stage(value: Any) -> str:
    """Normalize the several labels used for the same funding stage."""

    stage = _normalized(value).replace("_", " ")
    aliases = {
        "pre seed": "s",
        "pre-seed": "s",
        "seed": "s",
        "series a": "a",
        "series b": "b",
        "series c": "c",
        "series d": "d",
        "series e": "e",
        "series f": "f",
        "series g or later": "x",
        "series g": "x",
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
    if isinstance(value, dict):
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
    require_public_company: bool | None = None
    work_modes: frozenset[str] = frozenset()
    countries: frozenset[str] = frozenset()
    regions: frozenset[str] = frozenset()
    remote_friendly: bool | None = None
    max_in_office_days: int | None = None
    industries: frozenset[str] = frozenset()
    funding_stages: frozenset[str] = frozenset()
    max_cliff_months: int | None = None
    max_vesting_months: int | None = None
    prefer_accelerated: bool | None = None
    culture_tags: frozenset[str] = frozenset()
    priorities: dict[str, float] = field(default_factory=dict)
    #: Per-criterion hard/soft importance weights (criterion path -> 0.0..1.0).
    importance: dict[str, float] = field(default_factory=dict)
    #: The user-declared applicable scope (countries and role families).
    scope_countries: frozenset[str] = frozenset()
    scope_role_families: frozenset[str] = frozenset()
    criteria_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "priorities", dict(sorted(self.priorities.items())))
        object.__setattr__(self, "importance", dict(sorted(self.importance.items())))


def _importance_map(value: Any) -> dict[str, float]:
    """Project the ``importance`` float_map, dropping malformed entries."""
    result: dict[str, float] = {}
    if isinstance(value, Mapping):
        for key, weight in value.items():
            number = _decimal(weight)
            if number is not None and isinstance(key, str) and key:
                result[key] = float(max(0.0, min(1.0, number)))
    return result


def _scope_values(scope: Any, name: str) -> frozenset[str]:
    """Project one user-declared scope dimension (countries/role families)."""
    if not isinstance(scope, Mapping):
        return frozenset()
    return _strings(scope.get(name))


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
    scope = prefs.get("scope") or {}
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
        require_public_company=_boolean(compensation.get("require_public_company")),
        work_modes=modes,
        countries=_strings(work_location.get("countries")),
        regions=_strings(geography.get("regions")),
        remote_friendly=_boolean(geography.get("remote_friendly")),
        max_in_office_days=_integer(work_location.get("max_in_office_days")),
        industries=_strings(prefs.get("industry")),
        funding_stages=_strings(prefs.get("funding_stage")),
        max_cliff_months=_integer(vesting.get("max_cliff_months")),
        max_vesting_months=_integer(vesting.get("max_vesting_months")),
        prefer_accelerated=_boolean(vesting.get("prefer_accelerated")),
        culture_tags=_strings(prefs.get("culture")),
        priorities=raw_priorities,
        importance=_importance_map(prefs.get("importance")),
        scope_countries=_scope_values(scope, "countries"),
        scope_role_families=_scope_values(scope, "role_families"),
        criteria_version=int(schema_version),
    )


class ExclusionReason(str, Enum):
    EXCLUDED_COMPANY = "excluded_company"
    EXCLUDED_TITLE = "excluded_title"
    EXCLUDED_INDUSTRY = "excluded_industry"
    EXCLUDED_LOCATION = "excluded_location"
    INACTIVE_LISTING = "inactive_listing"
    NO_ORGANIZATION = "no_organization"
    NOT_PUBLIC_COMPANY = "not_public_company"
    RTO_EXCEEDS_MAXIMUM = "rto_exceeds_maximum"
    MINIMUM_SALARY_NOT_MET = "minimum_salary_not_met"
    REQUIREMENT_NOT_MET = "requirement_not_met"
    REQUIREMENT_UNVERIFIED = "requirement_unverified"


@dataclass(frozen=True)
class FactorContribution:
    factor: str
    score: float
    max_score: float
    detail: str


#: Requirement status values.
MATCH = "match"
MISMATCH = "mismatch"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class RequirementOutcome:
    """One evaluated requirement: status, observed value, and source reference."""

    path: str
    status: str  # ``match`` | ``mismatch`` | ``unknown``
    observed: Any  # observed value (or ``None``)
    source_kind: str | None  # ``evidence`` | ``field`` | ``None``
    source_id: Any  # evidence id (int) or direct-field name (str); ``None``
    scope_ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "observed": self.observed,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "scope_ok": self.scope_ok,
        }


@dataclass(frozen=True)
class MatchResult:
    listing_id: int
    score: float
    excluded: bool
    exclusion_reasons: list[str]
    factors: list[FactorContribution]
    ranker_version: str
    criteria_version: int
    requirements: list[RequirementOutcome] = field(default_factory=list)


def _contains(text: str, candidates: Iterable[str]) -> bool:
    return any(candidate and candidate in text for candidate in candidates)


def _metadata(listing: Any, organization: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source in (getattr(listing, "source_metadata", None), getattr(organization, "source_metadata", None)):
        if isinstance(source, dict):
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


def _organization_value_source(
    listing: Any,
    organization: Any,
    *names: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[Any, str | None]:
    """Like :func:`_organization_value` but also names the supplying field.

    Returns ``(value, source_id)`` where ``source_id`` is the model field
    (``organization.<name>``) or merged metadata key
    (``source_metadata.<name>``) that actually supplied the value, so a
    requirement outcome can report the field that justified the observation
    instead of a guessed ``organization.*``/``listing.*`` name. ``metadata``
    may be precomputed once per listing with :func:`_metadata` to avoid the
    redundant per-call merge.
    """
    if metadata is None:
        metadata = _metadata(listing, organization)
    for name in names:
        value = getattr(organization, name, None)
        if value not in (None, ""):
            return value, f"organization.{name}"
        if name in metadata and metadata[name] not in (None, ""):
            return metadata[name], f"source_metadata.{name}"
    return None, None


def _contains_token(haystack: str, term: str) -> bool:
    """Whole-token containment: a term matches only on word boundaries.

    The previous ``term in haystack`` substring test had false positives such
    as scope country ``US`` matching listing location ``Russia`` (``'us' in
    'russia'``). A ``\\b``-bounded match means ``us`` matches a standalone
    ``us`` token but never the interior of ``russia``.
    """
    if not haystack or not term:
        return False
    return re.search(r"\b" + re.escape(term) + r"\b", haystack) is not None


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
    for _field, maximum in (
        ("cliff_months", criteria.max_cliff_months),
        ("vesting_months", criteria.max_vesting_months),
    ):
        if maximum is None:
            continue
        actual = _decimal(_organization_value(listing, organization, _field, f"max_{_field}"))
        if actual is not None:
            checks.append(1.0 if actual <= maximum else 0.0)
            details.append(f"{_field}={actual}/{maximum}")
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


#: Human-readable labels used by the single reason renderer.
_FUNDING_LABELS = {
    "S": "Seed",
    "A": "Series A",
    "B": "Series B",
    "C": "Series C",
    "D": "Series D",
    "E": "Series E",
    "F": "Series F",
    "X": "Series G+",
    "O": "Other Private",
    "P": "Public",
}
_RTO_LABELS = {"R": "Remote", "H": "Hybrid", "O": "In-office"}

#: Canonical criterion paths evaluated, in deterministic order.
REQUIREMENT_ORDER = (
    "compensation.minimum_salary",
    "compensation.equity_minimum_percent",
    "compensation.require_public_company",
    "work_location.modes",
    "work_location.countries",
    "work_location.max_in_office_days",
    "geography.regions",
    "geography.remote_friendly",
    "industry",
    "funding_stage",
    "culture",
    "vesting.max_cliff_months",
    "vesting.max_vesting_months",
    "vesting.prefer_accelerated",
)

#: Which criterion path may be backed by accepted ``CompanyFieldEvidence`` and,
#: if so, under which field key.
_EVIDENCE_FIELD_KEY = {
    "compensation.require_public_company": "public_status",
    "work_location.modes": "rto_policy",
    "work_location.max_in_office_days": "rto_policy",
    "work_location.countries": "locations",
    "funding_stage": "funding_round",
    "vesting.prefer_accelerated": "accelerated_vesting",
}

#: Requirements that are hard whether or not ``importance`` names them.
_ALWAYS_HARD = frozenset(
    {"compensation.require_public_company", "work_location.max_in_office_days"}
)


def _is_hard(criteria: JobCriteria, path: str) -> bool:
    if path in _ALWAYS_HARD:
        return True
    return criteria.importance.get(path, 0.0) >= 1.0


def _evidence_in_scope(evidence_row: Any, listing: Any) -> bool:
    """Return whether an evidence row's declared scope covers this listing."""
    scope = getattr(evidence_row, "scope_json", None)
    if not isinstance(scope, Mapping):
        return True
    countries = scope.get("countries")
    if countries:
        location = _normalized(getattr(listing, "location_text", ""))
        if not any(_contains_token(location, _normalized(c)) for c in _values(countries) if c):
            return False
    role_families = scope.get("role_families")
    if role_families:
        title = _normalized(getattr(listing, "title", ""))
        if not any(_contains_token(title, _normalized(r)) for r in _values(role_families) if r):
            return False
    return True


def _rto_days(value: Any) -> int | None:
    """Map an RTO policy (code, label, or producer prose) to in-office days."""
    normalized = _normalized(value).replace("_", " ")
    mapping = {
        "r": 0, "remote": 0,
        "h": 3, "hybrid": 3,
        "o": 5, "in-office": 5, "in office": 5, "onsite": 5,
    }
    if normalized in mapping:
        return mapping[normalized]
    # CompanyFieldEvidence stores prose (e.g. "Remote first", "Hybrid 3 days",
    # "Five days in office, no exceptions"). Match the strongest explicit
    # signal; an explicit office term wins, then hybrid, then remote.
    if any(token in normalized for token in ("in-office", "in office", "onsite", "office")):
        return 5
    if "hybrid" in normalized:
        return 3
    if "remote" in normalized:
        return 0
    return None


def _mode_from_rto(value: Any) -> str | None:
    """Map a normalized RTO value to a work mode."""
    return {0: "remote", 3: "hybrid", 5: "in-office"}.get(_rto_days(value))


def _public_status(value: Any) -> bool | None:
    """Classify public-status ``value_text`` prose as public / private / unknown.

    ``CompanyFieldEvidence`` for ``public_status`` stores prose such as
    ``Public company`` or ``Private company``; a value that cannot be classified
    yields ``None`` (unknown).
    """
    text = _normalized(value).replace("_", " ")
    if not text:
        return None
    negative = ("private", "privately held", "closely held", "not public")
    positive = ("public", "publicly", "listed", "ticker", "nasdaq", "nyse", "ipo")
    for token in negative:
        if token in text:
            return False
    for token in positive:
        if token in text:
            return True
    return None


def _resolve_value(
    listing: Any,
    organization: Any,
    evidence: Mapping[str, Any] | None,
    field_key: str | None,
    direct_names: tuple[str, ...],
    *,
    default_guard: Any = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[Any, str, Any, bool] | None:
    """Resolve a requirement datum: accepted evidence first, then a direct field.

    Returns ``(observed, source_kind, source_id, scope_ok)`` or ``None`` when
    the datum is absent or falls back to an unstated model default (so it can
    never establish a verified result). ``default_guard`` marks a direct value
    equal to a model default as unknown. ``metadata`` may be precomputed once
    per listing with :func:`_metadata`.
    """
    if evidence and field_key and field_key in evidence:
        row = evidence[field_key]
        return (
            row.value_text,
            "evidence",
            row.pk,
            _evidence_in_scope(row, listing),
        )
    value, src = _organization_value_source(listing, organization, *direct_names, metadata=metadata)
    if value in (None, ""):
        return None
    if default_guard is not None and default_guard(value):
        return None
    return value, "field", src, True


def _equity_value(listing: Any, organization: Any, *, metadata: dict[str, Any] | None = None) -> Any:
    if metadata is None:
        metadata = _metadata(listing, organization)
    return metadata.get("equity_percent")


def _requirement_set_paths(criteria: JobCriteria) -> tuple[str, ...]:
    """Return the criterion paths whose *value* is actually set, in order.

    ``importance`` is a weight, not a value: a document that names a criterion
    in ``importance`` without its value (e.g. importance.minimum_salary=1.0
    with no ``minimum_salary``) must not be evaluated — it previously reached
    the evaluator with ``threshold=None`` and raised a bare ``TypeError``.
    """
    paths: list[str] = []
    if criteria.min_salary is not None:
        paths.append("compensation.minimum_salary")
    if criteria.equity_minimum is not None:
        paths.append("compensation.equity_minimum_percent")
    if criteria.require_public_company is True:
        paths.append("compensation.require_public_company")
    if criteria.work_modes:
        paths.append("work_location.modes")
    if criteria.countries:
        paths.append("work_location.countries")
    if criteria.max_in_office_days is not None:
        paths.append("work_location.max_in_office_days")
    if criteria.regions:
        paths.append("geography.regions")
    if criteria.remote_friendly is not None:
        paths.append("geography.remote_friendly")
    if criteria.industries:
        paths.append("industry")
    if criteria.funding_stages:
        paths.append("funding_stage")
    if criteria.culture_tags:
        paths.append("culture")
    if criteria.max_cliff_months is not None:
        paths.append("vesting.max_cliff_months")
    if criteria.max_vesting_months is not None:
        paths.append("vesting.max_vesting_months")
    if criteria.prefer_accelerated is not None:
        paths.append("vesting.prefer_accelerated")
    return tuple(paths)


def _eval_requirement(
    path: str, listing: Any, organization: Any, criteria: JobCriteria, evidence: Mapping[str, Any] | None,
    *,
    metadata: dict[str, Any] | None = None,
) -> RequirementOutcome | None:
    """Evaluate one canonical requirement into a :class:`RequirementOutcome`."""
    fk = _EVIDENCE_FIELD_KEY.get(path)

    if path == "compensation.minimum_salary":
        minimum = _decimal(getattr(listing, "compensation_min", None))
        maximum = _decimal(getattr(listing, "compensation_max", None))
        threshold = criteria.min_salary
        if threshold is None:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        currency = _text(getattr(listing, "compensation_currency", "")).upper()
        if currency and currency != criteria.currency:
            # Amounts in different currencies are incomparable: a hard salary
            # in the user's currency can never be verified against a listing
            # published in another.
            return RequirementOutcome(path, MISMATCH, currency, "field", "listing.compensation_currency")
        if minimum is None and maximum is None:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        if minimum is not None and minimum >= threshold:
            return RequirementOutcome(path, MATCH, int(minimum), "field", "listing.compensation_min")
        if maximum is not None and maximum < threshold:
            return RequirementOutcome(path, MISMATCH, int(maximum), "field", "listing.compensation_max")
        return RequirementOutcome(path, UNKNOWN, None, None, None)

    if path == "compensation.equity_minimum_percent":
        equity = _decimal(_equity_value(listing, organization, metadata=metadata))
        if equity is None:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        threshold = Decimal(str(criteria.equity_minimum))
        status = MATCH if equity >= threshold else MISMATCH
        return RequirementOutcome(path, status, float(equity), "field", "source_metadata.equity_percent")

    if path == "compensation.require_public_company":
        # Public status is its own evidence field (``public_status``), whose
        # prose ("Public company" / "Private company") is the direct answer.
        # ``funding_round`` evidence ("Series A", "Seed") says nothing about
        # public status, so it must never back this requirement.
        if evidence and fk and fk in evidence:
            row = evidence[fk]
            if not _evidence_in_scope(row, listing):
                return RequirementOutcome(path, UNKNOWN, None, "evidence", row.pk, scope_ok=False)
            is_public = _public_status(row.value_text)
            if is_public is None:
                return RequirementOutcome(path, UNKNOWN, None, "evidence", row.pk)
            status = MATCH if is_public else MISMATCH
            return RequirementOutcome(path, status, row.value_text, "evidence", row.pk)
        # Direct: ``funding_round == "P"`` is the model default, so it cannot
        # verify public status; an explicit private stage is a verified "not
        # public".
        resolved = _resolve_value(
            listing, organization, None, None, ("funding_round",),
            default_guard=lambda v: _normalized(v) == "p",
            metadata=metadata,
        )
        if resolved is None:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        observed, sk, sid, scope_ok = resolved
        status = MATCH if str(observed) == "P" else MISMATCH
        return RequirementOutcome(path, status, str(observed), sk, sid)

    if path == "work_location.modes":
        if evidence and fk and fk in evidence:
            row = evidence[fk]
            if not _evidence_in_scope(row, listing):
                return RequirementOutcome(path, UNKNOWN, None, "evidence", row.pk, scope_ok=False)
            mode = _mode_from_rto(row.value_text)
            if mode is None:
                return RequirementOutcome(path, UNKNOWN, None, "evidence", row.pk)
            status = MATCH if mode in criteria.work_modes else MISMATCH
            return RequirementOutcome(path, status, mode, "evidence", row.pk)
        is_remote = getattr(listing, "is_remote", None)
        if is_remote is True:
            status = MATCH if "remote" in criteria.work_modes else MISMATCH
            return RequirementOutcome(path, status, "remote", "field", "listing.is_remote")
        # Non-remote or unknown: refine via the organization RTO policy, but an
        # unstated default ``H`` must never establish a "hybrid" match (AC-5).
        resolved = _resolve_value(
            listing, organization, None, None, ("rto_policy",),
            default_guard=lambda v: _normalized(v) == "h",
            metadata=metadata,
        )
        if resolved is None:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        observed, sk, sid, scope_ok = resolved
        mode = _mode_from_rto(observed)
        if mode is None:
            return RequirementOutcome(path, UNKNOWN, None, sk, sid)
        status = MATCH if mode in criteria.work_modes else MISMATCH
        return RequirementOutcome(path, status, mode, sk, sid)

    if path == "work_location.countries":
        # Accepted company "locations" evidence first, then the listing's own
        # ``location_text`` — a listing attribute the organization resolver
        # never reads.
        observed: Any = None
        sk: Any = None
        sid: Any = None
        if evidence and fk and fk in evidence:
            row = evidence[fk]
            if not _evidence_in_scope(row, listing):
                return RequirementOutcome(path, UNKNOWN, None, "evidence", row.pk, scope_ok=False)
            observed, sk, sid = row.value_text, "evidence", row.pk
        else:
            observed = getattr(listing, "location_text", None)
            if observed not in (None, ""):
                sk, sid = "field", "listing.location_text"
        if observed in (None, ""):
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        location = _normalized(observed)
        matched = [c for c in criteria.countries if _normalized(c) and _normalized(c) in location]
        status = MATCH if matched else MISMATCH
        return RequirementOutcome(path, status, matched[0] if matched else observed, sk, sid)

    if path == "work_location.max_in_office_days":
        resolved = _resolve_value(
            listing, organization, evidence, fk, ("rto_policy",),
            default_guard=lambda v: v == "H",
            metadata=metadata,
        )
        if resolved is None:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        observed, sk, sid, scope_ok = resolved
        if not scope_ok:
            return RequirementOutcome(path, UNKNOWN, None, sk, sid, scope_ok=False)
        days = _rto_days(observed)
        if days is None:
            return RequirementOutcome(path, UNKNOWN, None, sk, sid)
        status = MATCH if days <= criteria.max_in_office_days else MISMATCH
        return RequirementOutcome(path, status, days, sk, sid)

    if path == "geography.regions":
        location = _normalized(getattr(listing, "location_text", ""))
        if not location:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        matched = [r for r in criteria.regions if _normalized(r) and _normalized(r) in location]
        status = MATCH if matched else MISMATCH
        return RequirementOutcome(path, status, matched[0] if matched else None, "field", "listing.location_text")

    if path == "geography.remote_friendly":
        remote = getattr(listing, "is_remote", None)
        if not isinstance(remote, bool):
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        status = MATCH if remote == criteria.remote_friendly else MISMATCH
        return RequirementOutcome(path, status, remote, "field", "listing.is_remote")

    if path == "industry":
        value, src = _organization_value_source(listing, organization, "industry", "industries", metadata=metadata)
        actual = _strings(value)
        if not actual:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        matched = sorted(actual & criteria.industries)
        status = MATCH if matched else MISMATCH
        observed = matched[0] if matched else (sorted(actual)[0] if actual else None)
        return RequirementOutcome(path, status, observed, "field", src)

    if path == "funding_stage":
        resolved = _resolve_value(
            listing, organization, evidence, fk, ("funding_round", "funding_stage"),
            default_guard=lambda v: v == "P",
            metadata=metadata,
        )
        if resolved is None:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        observed, sk, sid, scope_ok = resolved
        if not scope_ok:
            return RequirementOutcome(path, UNKNOWN, None, sk, sid, scope_ok=False)
        stage = _canonical_stage(observed)
        wanted = frozenset(_canonical_stage(s) for s in criteria.funding_stages)
        status = MATCH if stage in wanted else MISMATCH
        return RequirementOutcome(path, status, stage, sk, sid)

    if path == "culture":
        tags_value, tag_src = _organization_value_source(listing, organization, "culture_tags", "culture", metadata=metadata)
        tags = _strings(tags_value)
        haystack = " ".join((_text(getattr(listing, "description_excerpt", "")), _text((metadata or {}).get("culture"))))
        matched = [tag for tag in criteria.culture_tags if tag in haystack or tag in tags]
        if not haystack.strip() and not tags:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        status = MATCH if matched else MISMATCH
        observed = matched[0] if matched else (sorted(tags)[0] if tags else None)
        # Cite the field that supplied the value: organization culture tags
        # when they carried the match, otherwise the listing description.
        if matched and tag_src and matched[0] in tags:
            source = tag_src
        else:
            source = "listing.description_excerpt"
        return RequirementOutcome(path, status, observed, "field", source)

    if path in ("vesting.max_cliff_months", "vesting.max_vesting_months"):
        field_name = "cliff_months" if path == "vesting.max_cliff_months" else "vesting_months"
        maximum = criteria.max_cliff_months if path == "vesting.max_cliff_months" else criteria.max_vesting_months
        value, src = _organization_value_source(listing, organization, field_name, f"max_{field_name}", metadata=metadata)
        actual = _decimal(value)
        if actual is None:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        status = MATCH if actual <= maximum else MISMATCH
        return RequirementOutcome(path, status, int(actual), "field", src)

    if path == "vesting.prefer_accelerated":
        resolved = _resolve_value(listing, organization, evidence, fk, ("accelerated_vesting",), metadata=metadata)
        if resolved is None:
            return RequirementOutcome(path, UNKNOWN, None, None, None)
        observed, sk, sid, scope_ok = resolved
        if not scope_ok:
            return RequirementOutcome(path, UNKNOWN, None, sk, sid, scope_ok=False)
        accelerated = observed is True or str(observed).strip().lower() in {"true", "yes", "1"}
        status = MATCH if accelerated == criteria.prefer_accelerated else MISMATCH
        return RequirementOutcome(path, status, accelerated, sk, sid)

    return None


def evaluate_requirements(
    listing: Any,
    criteria: JobCriteria,
    *,
    organization: Any = None,
    evidence: Mapping[str, Any] | None = None,
    paths: tuple[str, ...] | None = None,
) -> list[RequirementOutcome]:
    """Evaluate every user-set canonical requirement for one listing.

    Returns a :class:`RequirementOutcome` per set requirement in
    :data:`REQUIREMENT_ORDER` order. Accepted evidence resolves first, then a
    documented direct field; an unstated model default or absent datum yields
    ``unknown`` and can never produce a verified ``match``. ``paths`` may be
    precomputed with :func:`_requirement_set_paths` to avoid recomputing it per
    listing in a bulk ranking pass.
    """
    organization = organization if organization is not None else getattr(listing, "organization", None)
    if paths is None:
        paths = _requirement_set_paths(criteria)
    metadata = _metadata(listing, organization)
    outcomes: list[RequirementOutcome] = []
    for path in paths:
        outcome = _eval_requirement(path, listing, organization, criteria, evidence, metadata=metadata)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def hard_exclusion_reasons(
    outcomes: Iterable[RequirementOutcome], criteria: JobCriteria
) -> list[ExclusionReason]:
    """Derive exclusion reasons from hard requirements that failed or are unknown."""
    reasons: list[ExclusionReason] = []
    for outcome in outcomes:
        if not _is_hard(criteria, outcome.path):
            continue
        if outcome.status == MISMATCH:
            if outcome.path == "compensation.require_public_company":
                reasons.append(ExclusionReason.NOT_PUBLIC_COMPANY)
            elif outcome.path == "work_location.max_in_office_days":
                reasons.append(ExclusionReason.RTO_EXCEEDS_MAXIMUM)
            elif outcome.path == "compensation.minimum_salary":
                reasons.append(ExclusionReason.MINIMUM_SALARY_NOT_MET)
            else:
                reasons.append(ExclusionReason.REQUIREMENT_NOT_MET)
        elif outcome.status == UNKNOWN:
            reasons.append(ExclusionReason.REQUIREMENT_UNVERIFIED)
    return reasons


def _funding_label(code: Any) -> str:
    if not code:
        return "Unknown"
    return _FUNDING_LABELS.get(str(code).upper(), str(code))


def _rto_label(code: Any) -> str:
    if not code:
        return "Unknown"
    return _RTO_LABELS.get(str(code), str(code))


def _match_reason(outcome: RequirementOutcome) -> str | None:
    """Map one ``match`` outcome to a concise, human-readable reason."""
    path = outcome.path
    observed = outcome.observed
    if path == "compensation.minimum_salary":
        try:
            return f"Salary {int(observed):,}+"
        except (TypeError, ValueError):
            return "Salary meets minimum"
    if path == "compensation.equity_minimum_percent":
        return f"Equity {observed}%+"
    if path == "compensation.require_public_company":
        return "Public company"
    if path == "work_location.modes":
        return {"remote": "Remote", "hybrid": "Hybrid", "in-office": "In-office"}.get(_normalized(observed), None)
    if path == "work_location.countries":
        return f"Located in {observed}" if observed else None
    if path == "work_location.max_in_office_days":
        return None
    if path == "geography.regions":
        return f"In {observed}" if observed else None
    if path == "geography.remote_friendly":
        return "Remote-friendly"
    if path == "industry":
        return f"Industry: {observed}" if observed else None
    if path == "funding_stage":
        return _funding_label(observed)
    if path == "culture":
        return f"Culture: {observed}" if observed else None
    if path in ("vesting.max_cliff_months", "vesting.max_vesting_months"):
        return "Vesting aligns"
    if path == "vesting.prefer_accelerated":
        return "Accelerated vesting"
    return None


def coverage(outcomes: Iterable[RequirementOutcome]) -> float:
    """Fraction of evaluated requirements with a known value (not ``unknown``)."""
    outcomes = list(outcomes)
    if not outcomes:
        return 0.0
    known = sum(1 for o in outcomes if getattr(o, "status", UNKNOWN) != UNKNOWN)
    return round(known / len(outcomes), 4)


def reasons_from_requirements(outcomes: Iterable[RequirementOutcome]) -> list[str]:
    """Render requirement outcomes into ordered, deduplicated reasons (≤6).

    The single reason renderer shared by the API, persisted-match, and chat
    surfaces (issue #467 AC-1).
    """
    reasons: list[str] = []
    seen: set[str] = set()
    for outcome in outcomes:
        label = None
        try:
            if getattr(outcome, "status", UNKNOWN) == MATCH:
                label = _match_reason(outcome)
        except (AttributeError, TypeError):
            label = None
        if label and label not in seen:
            reasons.append(label)
            seen.add(label)
        if len(reasons) >= 6:
            break
    return reasons


def _excluded(
    listing: Any, criteria: JobCriteria, outcomes: list[RequirementOutcome]
) -> list[ExclusionReason]:
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
    if reasons:
        return reasons
    reasons.extend(hard_exclusion_reasons(outcomes, criteria))
    return reasons


def rank_listing(
    listing: Any,
    criteria: JobCriteria,
    config: RankingConfig = DEFAULT_CONFIG,
    *,
    evidence: Mapping[str, Any] | None = None,
    paths: tuple[str, ...] | None = None,
) -> MatchResult:
    """Apply hard exclusions, then calculate deterministic factor contributions."""

    organization = getattr(listing, "organization", None)
    outcomes = evaluate_requirements(
        listing, criteria, organization=organization, evidence=evidence, paths=paths
    )
    reasons = _excluded(listing, criteria, outcomes)
    listing_id = int(getattr(listing, "pk", getattr(listing, "id", 0)) or 0)
    if reasons:
        return MatchResult(
            listing_id, 0.0, True,
            [reason.value for reason in reasons], [],
            config.version, criteria.criteria_version, outcomes,
        )
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
    return MatchResult(
        listing_id, round(score, 10), False, [], factors,
        config.version, criteria.criteria_version, outcomes,
    )


def rank_listings(
    listings: Iterable[Any],
    criteria: JobCriteria,
    config: RankingConfig = DEFAULT_CONFIG,
    *,
    evidence: Mapping[int, Mapping[str, Any]] | None = None,
) -> list[MatchResult]:
    """Rank listings by descending score and ascending ID for ties."""

    evidence = evidence or {}
    paths = _requirement_set_paths(criteria)

    def _evidence_for(listing: Any) -> Mapping[str, Any]:
        organization = getattr(listing, "organization", None)
        org_id = int(getattr(organization, "pk", 0) or 0)
        return evidence.get(org_id) or {}

    results = [
        rank_listing(listing, criteria, config, evidence=_evidence_for(listing), paths=paths)
        for listing in listings
    ]
    return sorted(results, key=lambda result: (-result.score, result.listing_id))


__all__ = [
    "ExclusionReason", "FactorContribution", "JobCriteria", "MatchResult",
    "MATCH", "MISMATCH", "UNKNOWN", "REQUIREMENT_ORDER",
    "RequirementOutcome", "evaluate_requirements", "hard_exclusion_reasons",
    "reasons_from_requirements",
    "coverage",
    "project_criteria", "rank_listing", "rank_listings",
]
