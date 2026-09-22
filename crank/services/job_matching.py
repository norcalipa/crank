# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Preference-grounded matching service.

Turns saved user preferences into ranked job/company results with
human-readable match reasons.  No LLM is required for this layer.

Public API:
    match_jobs(user, limit=25) -> list[JobMatchResult]
    match_organizations(user, limit=25) -> list[OrgMatchResult]

Both functions:
1.  Read the user's preference document.
2.  Project it into :class:`JobCriteria` via :func:`project_criteria`.
3.  Apply hard filters (public-only, RTO ceiling, exclusions).
4.  Score survivors with the deterministic ranking engine.
5.  Attach human-readable reason strings.
6.  Return a bounded, stable-ordered list.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from django.utils import timezone

from crank.agents.jobs.matching import (
    JobCriteria,
    MatchResult,
    RequirementOutcome,
    _canonical_stage,
    _decimal,
    _normalized,
    _organization_value,
    coverage,
    evaluate_requirements,
    hard_exclusion_reasons,
    project_criteria,
    reasons_from_requirements,
)
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG, RankingConfig
from crank.models.job import JobListing
from crank.models.organization import Organization
from crank.models.preference import UserPreference

#: Maximum number of results returned by the matching service.
MAX_MATCH_RESULTS = 25

#: RTO policy labels for human-readable reasons.
_RTO_LABELS = {"R": "Remote", "H": "Hybrid", "O": "In-office"}

#: Funding round labels for human-readable reasons.
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


@dataclass(frozen=True)
class JobMatchResult:
    """A ranked job-listing match with human-readable reasons."""

    listing_id: int
    title: str
    employer_name: str
    organization_id: int | None
    organization_name: str
    canonical_url: str
    location_text: str
    is_remote: bool
    score: float
    reasons: list[str] = field(default_factory=list)
    factors: list[dict] = field(default_factory=list)
    # issue #467: separate figures + revision block.
    fit_score: float | None = None
    company_score: float | None = None
    coverage: float = 0.0
    requirements: list[dict] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    evidence_ids: list[int] = field(default_factory=list)
    preference_revision: int | None = None
    ranking_version: str = ""
    data_revision: int | None = None
    generated_at: Any | None = None
    stale: bool = False

    def revision(self) -> dict[str, Any]:
        return {
            "preference_revision": self.preference_revision,
            "ranking_version": self.ranking_version,
            "data_revision": self.data_revision,
            "generated_at": _iso_or_none(self.generated_at),
            "stale": self.stale,
        }


@dataclass(frozen=True)
class OrgMatchResult:
    """A ranked organization match with human-readable reasons."""

    organization_id: int
    name: str
    url: str
    funding_round: str
    rto_policy: str
    score: float
    reasons: list[str] = field(default_factory=list)
    # issue #467: separate figures + revision block.
    fit_score: float | None = None
    company_score: float | None = None
    coverage: float = 0.0
    requirements: list[dict] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    evidence_ids: list[int] = field(default_factory=list)
    preference_revision: int | None = None
    ranking_version: str = ""
    data_revision: int | None = None
    generated_at: Any | None = None
    stale: bool = False

    def revision(self) -> dict[str, Any]:
        return {
            "preference_revision": self.preference_revision,
            "ranking_version": self.ranking_version,
            "data_revision": self.data_revision,
            "generated_at": _iso_or_none(self.generated_at),
            "stale": self.stale,
        }


def _iso_or_none(value: Any) -> str | None:
    """Return an ISO-8601 string for a datetime, or ``None``."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Reason generation
# ---------------------------------------------------------------------------


def _funding_label(code: Any) -> str:
    if not code:
        return "Unknown"
    return _FUNDING_LABELS.get(str(code), str(code))


def _rto_label(code: Any) -> str:
    if not code:
        return "Unknown"
    return _RTO_LABELS.get(str(code), str(code))


def _company_score(organization: Any) -> float | None:
    """The organization's average score (0-5 scale), or ``None`` when unknown."""
    if organization is None:
        return None
    try:
        scores = organization.avg_scores()
    except (AttributeError, TypeError, ValueError):
        return None
    values: list[float] = []
    for row in scores or ():
        if not isinstance(row, dict):
            continue
        value = _decimal(row.get("avg_score", row.get("score")))
        if value is not None:
            values.append(float(value))
    if not values:
        return None
    return max(0.0, min(5.0, sum(values) / len(values)))


def _org_listing_view(organization: Any) -> Any:
    """A minimal listing-shaped view of an organization for requirement eval."""
    from types import SimpleNamespace

    return SimpleNamespace(
        pk=getattr(organization, "pk", None),
        id=getattr(organization, "pk", None),
        organization=organization,
        employer_name=getattr(organization, "name", ""),
        title="",
        location_text="",
        is_remote=None,
        compensation_min=None,
        compensation_max=None,
        compensation_currency="",
        description_excerpt="",
        source_metadata={},
        status="active",
    )


def evaluate_org_requirements(
    organization: Any, criteria: JobCriteria, evidence: Any | None = None
) -> list[RequirementOutcome]:
    """Evaluate user-set requirements against an organization (no listing data)."""
    return evaluate_requirements(
        _org_listing_view(organization),
        criteria,
        organization=organization,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Organization scoring (independent of job listings)
# ---------------------------------------------------------------------------


def _score_organization_match(
    organization: Any,
    criteria: JobCriteria,
    config: RankingConfig,
) -> float:
    """Score an organization independently of any specific listing."""
    score = 0.0
    max_score = float(config.max_score)

    # Funding stage match (hard filter already applied, so just score)
    funding = getattr(organization, "funding_round", None)
    if funding and criteria.funding_stages:
        normalized = _canonical_stage(funding)
        wanted = frozenset(_canonical_stage(s) for s in criteria.funding_stages)
        if normalized in wanted:
            score += max_score * float(config.weights.get("funding_stage", 0.0))

    # RTO / work location
    rto = getattr(organization, "rto_policy", None)
    if rto and criteria.work_modes:
        mode_map = {"R": "remote", "H": "hybrid", "O": "in-office"}
        mode = mode_map.get(rto, "")
        if mode in criteria.work_modes:
            score += max_score * float(config.weights.get("work_location", 0.0))

    # Industry
    org_industries = _strings_safe(getattr(organization, "industry", ""))
    if org_industries and criteria.industries and (org_industries & criteria.industries):
            score += max_score * float(config.weights.get("industry", 0.0))

    # Organization scores
    try:
        scores = organization.avg_scores()
        values: list[float] = []
        for row in scores or ():
            if isinstance(row, dict):
                val = _decimal(row.get("avg_score", row.get("score")))
                if val is not None:
                    values.append(float(val))
        if values:
            avg = max(0.0, min(5.0, sum(values) / len(values)))
            weight = float(config.weights.get("organization_scores", 0.0))
            score += max_score * weight * (avg / 5.0)
    except (AttributeError, TypeError, ValueError):
        pass

    # Vesting
    has_vesting_pref = any(v is not None for v in (
        criteria.prefer_accelerated,
        criteria.max_cliff_months,
        criteria.max_vesting_months,
    ))
    if has_vesting_pref:
        checks: list[float] = []
        if criteria.prefer_accelerated is not None:
            accelerated = getattr(organization, "accelerated_vesting", None)
            if isinstance(accelerated, bool):
                checks.append(
                    1.0 if accelerated == criteria.prefer_accelerated else 0.0
                )
        if checks:
            weight = float(config.weights.get("vesting", 0.0))
            score += max_score * weight * (sum(checks) / len(checks))

    return round(min(max_score, score), 10)


def _strings_safe(value: Any) -> frozenset[str]:
    """Convert a value to a frozenset of normalized strings."""
    from crank.agents.jobs.matching import _strings
    return _strings(value)


# ---------------------------------------------------------------------------
# Hard filters for organizations
# ---------------------------------------------------------------------------


def _org_excluded(
    organization: Any, criteria: JobCriteria, evidence: Any | None = None
) -> bool:
    """Check if an organization is excluded by hard filters (evidence-aware)."""
    name = _normalized(getattr(organization, "name", ""))
    if name and name in criteria.excluded_companies:
        return True
    industry = _strings_safe(getattr(organization, "industry", ""))
    if industry and industry & criteria.excluded_industries:
        return True
    outcomes = evaluate_org_requirements(organization, criteria, evidence)
    return bool(hard_exclusion_reasons(outcomes, criteria))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _get_criteria(user, preferences_override=None) -> JobCriteria | None:
    """Read the user's preference document and project it to JobCriteria.

    ``preferences_override`` (issue #466) supplies an in-memory effective
    document (e.g. a this-search-only patch result) to drive one search
    without touching the stored canonical row; when given, no preference
    row is read at all.
    """
    if preferences_override is not None:
        from crank.models.preference import SCHEMA_VERSION as _PREF_SCHEMA

        return project_criteria(preferences_override, _PREF_SCHEMA)
    try:
        pref = UserPreference.objects.get(user=user)
    except UserPreference.DoesNotExist:
        return None
    return project_criteria(pref.preferences, pref.schema_version)


def _match_context(user, preferences_override=None):
    """Return ``(criteria, unsupported, preference_revision)`` for one pass."""
    from crank.services.preferences import unsupported_criteria

    if preferences_override is not None:
        from crank.models.preference import SCHEMA_VERSION as _PREF_SCHEMA

        criteria = project_criteria(preferences_override, _PREF_SCHEMA)
        return criteria, unsupported_criteria(preferences_override), None
    try:
        pref = UserPreference.objects.get(user=user)
    except UserPreference.DoesNotExist:
        return None, [], None
    return (
        project_criteria(pref.preferences, pref.schema_version),
        unsupported_criteria(pref.preferences),
        pref.revision,
    )


def _org_ids_from(listings: Iterable[Any]) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for listing in listings:
        organization = getattr(listing, "organization", None)
        pk = getattr(organization, "pk", None)
        if pk is not None:
            key = int(pk)
            if key not in seen:
                seen.add(key)
                ids.append(key)
    return ids


def _resolve_evidence(org_ids: list[int]) -> dict[int, dict[str, Any]]:
    from crank.services.company_evidence import resolve_field_evidence_for_orgs

    return resolve_field_evidence_for_orgs(org_ids)


# ---------------------------------------------------------------------------
# Bounded relaxation preview (issue #476)
# ---------------------------------------------------------------------------


#: Default probe cap when the caller does not pass one. Production callers
#: pass ``settings.JOB_MATCH_RELAXATION_PROBES``; this keeps the helper usable
#: standalone.
DEFAULT_RELAXATION_PROBES = 3

#: Deterministic probe order: highest-impact hard constraints first.
_RELAXATION_FIELDS = ("work_location", "minimum_salary", "exclusions")

#: Exclusion subsets probed for the concrete exclusions label, most common
#: first.  Values shown in the label are the user's own saved entries.
_EXCLUSION_TYPES = (
    ("excluded_companies", "companies"),
    ("excluded_titles", "job titles"),
    ("excluded_industries", "industries"),
    ("excluded_locations", "locations"),
)

_MODE_LABELS = {"remote": "Remote", "hybrid": "Hybrid", "in-office": "In-office"}


def _bounded_join(values: Any, limit: int = 3) -> str:
    """Bounded, human-readable list of the user's own saved values."""
    shown = [str(v) for v in list(values)[:limit]]
    text = ", ".join(shown)
    if len(values) > limit:
        text += f" and {len(values) - limit} more"
    return text

def _concrete_label(field_name: str, criteria: JobCriteria) -> str:
    """Name the concrete dimension being relaxed, with the current values.

    The contract requires a *specific* relaxation preview (e.g. "Broadening
    work location (currently Remote)"), never a generic "Removing
    exclusions".  Values come straight from the user's own saved criteria and
    are bounded in count and length.
    """
    if field_name == "work_location":
        label = "Broadening work location"
        parts: list[str] = []
        if criteria.work_modes:
            parts.append(
                ", ".join(
                    _MODE_LABELS.get(mode, str(mode).title())
                    for mode in sorted(criteria.work_modes)
                )
            )
        if criteria.countries:
            parts.append(_bounded_join(sorted(criteria.countries)))
        if parts:
            label += f" (currently {' · '.join(parts)})"
        if criteria.max_in_office_days is not None:
            label += f" (at most {criteria.max_in_office_days} in-office days)"
        return label
    if field_name == "minimum_salary" and criteria.min_salary is not None:
        try:
            return f"Lowering the {int(criteria.min_salary):,} minimum salary"
        except (TypeError, ValueError):
            return "Lowering the minimum salary"
    if field_name == "exclusions":
        for attr, type_label in _EXCLUSION_TYPES:
            values = getattr(criteria, attr, None)
            if values:
                return (
                    f"Removing excluded {type_label} "
                    f"(currently {_bounded_join(sorted(values))})"
                )
        return "Removing exclusions"
    return _RELAXATION_LABELS_FALLBACK.get(field_name, "Broadening your requirements")


#: Last-resort labels; reached only when a field has no concrete values to
#: name.  The probes above always produce concrete labels in practice.
_RELAXATION_LABELS_FALLBACK = {
    "work_location": "Broadening work location",
    "minimum_salary": "Lowering the minimum salary",
    "exclusions": "Removing exclusions",
}


def _constraint_is_set(criteria: JobCriteria, field_name: str) -> bool:
    """Return whether the constraint actually excludes anything right now."""
    if field_name == "work_location":
        return bool(
            criteria.work_modes
            or criteria.countries
            or criteria.max_in_office_days is not None
        )
    if field_name == "minimum_salary":
        return criteria.min_salary is not None
    if field_name == "exclusions":
        return bool(
            criteria.excluded_companies
            or criteria.excluded_titles
            or criteria.excluded_industries
            or criteria.excluded_locations
        )
    return False


def _relaxed_criteria(criteria: JobCriteria, field_name: str) -> JobCriteria:
    """Return a copy of *criteria* with one hard constraint relaxed.

    The saved preference document is never touched; this only relaxes the
    in-memory projection used for the probe.
    """
    if field_name == "work_location":
        return replace(
            criteria,
            work_modes=frozenset(),
            countries=frozenset(),
            max_in_office_days=None,
        )
    if field_name == "minimum_salary":
        return replace(criteria, min_salary=None)
    if field_name == "exclusions":
        return replace(
            criteria,
            excluded_companies=frozenset(),
            excluded_titles=frozenset(),
            excluded_industries=frozenset(),
            excluded_locations=frozenset(),
        )
    return criteria


def relaxation_preview(
    user,
    *,
    max_probes: int | None = None,
    limit: int = MAX_MATCH_RESULTS,
    config: RankingConfig = DEFAULT_CONFIG,
    queryset: Any = None,
) -> dict | None:
    """Probe bounded single-constraint relaxations of the user's criteria.

    Used only on a genuine zero-match: for at most ``max_probes`` set hard
    constraints (work-location modes/countries, minimum salary, exclusions —
    in that deterministic order), re-runs matching with that one constraint
    relaxed and reports the first that yields results.

    Read-only: the saved preference document is never read-modified-written,
    and no results are persisted. Returns ``None`` when the user has no
    saved criteria, the probe cap is zero, matching already yields results
    (nothing to explain), or no single relaxation helps.
    """
    criteria = _get_criteria(user)
    if criteria is None:
        return None
    cap = DEFAULT_RELAXATION_PROBES if max_probes is None else max(0, int(max_probes))
    if cap <= 0:
        return None
    capped = max(1, min(limit, MAX_MATCH_RESULTS))
    if queryset is None:
        queryset = JobListing.objects.select_related("organization").filter(
            status=JobListing.Status.ACTIVE
        )
    listings = list(queryset[: capped * 4])  # same over-fetch as match_jobs

    baseline = len(rank_listings_with_reasons(listings, criteria, config))
    if baseline > 0:
        return None

    probes = 0
    for field_name in _RELAXATION_FIELDS:
        if probes >= cap:
            break
        if not _constraint_is_set(criteria, field_name):
            continue
        probes += 1
        relaxed = _relaxed_criteria(criteria, field_name)
        count = len(rank_listings_with_reasons(listings, relaxed, config))
        if count > 0:
            return {
                "field": field_name,
                "label": _concrete_label(field_name, criteria),
                "added_count": min(count, capped),
            }
    return None


def match_jobs(
    user,
    *,
    limit: int = MAX_MATCH_RESULTS,
    config: RankingConfig = DEFAULT_CONFIG,
    queryset: Any = None,
    preferences_override: Any = None,
) -> list[JobMatchResult]:
    """Return ranked job-listing matches for *user*.

    1. Read preferences → JobCriteria.
    2. Query active job listings with organizations.
    3. Hard-filter (public-only, RTO ceiling, exclusions).
    4. Rank survivors with the deterministic engine.
    5. Attach human-readable reasons.
    6. Return a bounded, stable-ordered list.

    ``preferences_override`` is a thin additive seam (issue #466): an
    in-memory effective preference document drives this one search without
    a stored-preference read or write.
    """
    criteria, unsupported, preference_revision = _match_context(user, preferences_override)
    if criteria is None:
        return []

    capped = max(1, min(limit, MAX_MATCH_RESULTS))
    if queryset is None:
        queryset = JobListing.objects.select_related("organization").filter(
            status=JobListing.Status.ACTIVE
        )
    listings = list(queryset[:capped * 4])  # over-fetch before filtering

    org_ids = _org_ids_from(listings)
    evidence = _resolve_evidence(org_ids)
    from crank.services.publication import listing_data_revisions

    data_revisions = listing_data_revisions(listings)

    ranked = rank_listings_with_reasons(
        listings,
        criteria,
        config,
        unsupported=unsupported,
        preference_revision=preference_revision,
        evidence=evidence,
        data_revisions=data_revisions,
    )
    return ranked[:capped]


def match_organizations(
    user,
    *,
    limit: int = MAX_MATCH_RESULTS,
    config: RankingConfig = DEFAULT_CONFIG,
    queryset: Any = None,
    preferences_override: Any = None,
) -> list[OrgMatchResult]:
    """Return ranked organization matches for *user*.

    1. Read preferences → JobCriteria.
    2. Query active, public organizations.
    3. Hard-filter (public-only, RTO ceiling, exclusions).
    4. Score survivors.
    5. Attach human-readable reasons.
    6. Return a bounded, stable-ordered list.

    ``preferences_override`` is a thin additive seam (issue #466): an
    in-memory effective preference document drives this one search without
    a stored-preference read or write.
    """
    criteria, unsupported, preference_revision = _match_context(user, preferences_override)
    if criteria is None:
        return []

    capped = max(1, min(limit, MAX_MATCH_RESULTS))
    if queryset is None:
        queryset = Organization.objects.filter(status=1, public=True)
    orgs = list(queryset[:capped * 4])

    org_ids = {int(org.pk) for org in orgs if getattr(org, "pk", None)}
    evidence = _resolve_evidence(list(org_ids))
    from crank.services.publication import organization_data_revisions

    data_revisions = organization_data_revisions(list(org_ids))
    generated_at = timezone.now()

    survivors = [
        org for org in orgs if not _org_excluded(org, criteria, evidence.get(int(org.pk)))
    ]

    scored: list[tuple[float, int, OrgMatchResult]] = []
    for org in survivors:
        org_id = int(org.pk)
        outcomes = evaluate_org_requirements(org, criteria, evidence.get(org_id))
        org_evidence = evidence.get(org_id) or {}
        score = _score_organization_match(org, criteria, config)
        reasons = reasons_from_requirements(outcomes)
        company_score = _company_score(org)
        coverage_value = coverage(outcomes)
        evidence_ids = sorted(
            {ev.pk for ev in org_evidence.values() if getattr(ev, "pk", None) is not None}
        )
        result = OrgMatchResult(
            organization_id=org_id,
            name=str(org.name),
            url=str(getattr(org, "url", "")),
            funding_round=str(getattr(org, "funding_round", "")),
            rto_policy=str(getattr(org, "rto_policy", "")),
            score=score,
            reasons=reasons,
            fit_score=score,
            company_score=company_score,
            coverage=coverage_value,
            requirements=[o.as_dict() for o in outcomes],
            unsupported=list(unsupported),
            evidence_ids=evidence_ids,
            preference_revision=preference_revision,
            ranking_version=config.version,
            data_revision=data_revisions.get(org_id),
            generated_at=generated_at,
            stale=False,
        )
        scored.append((score, org_id, result))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:capped]]


def rank_listings_with_reasons(
    listings: Iterable[Any],
    criteria: JobCriteria,
    config: RankingConfig = DEFAULT_CONFIG,
    *,
    unsupported: Iterable[str] | None = None,
    preference_revision: int | None = None,
    evidence: Mapping[int, Mapping[str, Any]] | None = None,
    data_revisions: Mapping[int, int] | None = None,
) -> list[JobMatchResult]:
    """Rank listings and produce JobMatchResult objects with reasons.

    This is the shared ranking+reason engine used by both the API and chat
    tool. Reasons are rendered from the single :func:`reasons_from_requirements`
    pass, and each result carries the three separate figures plus the revision
    block (issue #467).
    """
    from crank.agents.jobs.matching import rank_listings

    unsupported = list(unsupported or [])
    data_revisions = data_revisions or {}
    evidence = evidence or {}
    generated_at = timezone.now()

    ranked: list[MatchResult] = rank_listings(listings, criteria, config, evidence=evidence)
    listing_by_id = {
        int(getattr(lst, "pk", getattr(lst, "id", 0)) or 0): lst
        for lst in listings
    }
    results: list[JobMatchResult] = []

    for match in ranked:
        if match.excluded:
            continue
        listing = listing_by_id.get(match.listing_id)
        if listing is None:
            continue
        organization = getattr(listing, "organization", None)
        org_id = (
            int(organization.pk)
            if organization and getattr(organization, "pk", None)
            else None
        )
        org_name = str(getattr(organization, "name", "")) if organization else ""
        org_evidence = evidence.get(org_id) or {} if org_id is not None else {}
        reasons = reasons_from_requirements(match.requirements)
        company_score = _company_score(organization)
        coverage_value = coverage(match.requirements)
        evidence_ids = sorted(
            {ev.pk for ev in org_evidence.values() if getattr(ev, "pk", None) is not None}
        )
        results.append(JobMatchResult(
            listing_id=match.listing_id,
            title=str(getattr(listing, "title", "")),
            employer_name=str(getattr(listing, "employer_name", "")),
            organization_id=org_id,
            organization_name=org_name,
            canonical_url=str(getattr(listing, "canonical_url", "")),
            location_text=str(getattr(listing, "location_text", "")),
            is_remote=bool(getattr(listing, "is_remote", False)),
            score=match.score,
            reasons=reasons,
            factors=[
                {
                    "factor": f.factor,
                    "score": f.score,
                    "max_score": f.max_score,
                    "detail": f.detail,
                }
                for f in match.factors
            ],
            fit_score=match.score,
            company_score=company_score,
            coverage=coverage_value,
            requirements=[o.as_dict() for o in match.requirements],
            unsupported=unsupported,
            evidence_ids=evidence_ids,
            preference_revision=preference_revision,
            ranking_version=config.version,
            data_revision=data_revisions.get(match.listing_id),
            generated_at=generated_at,
            stale=False,
        ))
    return results


__all__ = [
    "DEFAULT_RELAXATION_PROBES",
    "MAX_MATCH_RESULTS",
    "JobMatchResult",
    "OrgMatchResult",
    "evaluate_org_requirements",
    "match_jobs",
    "match_organizations",
    "rank_listings_with_reasons",
    "relaxation_preview",
]
