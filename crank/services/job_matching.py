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

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from crank.agents.jobs.matching import (
    _RTO_DAYS,
    FactorContribution,
    JobCriteria,
    MatchResult,
    _canonical_stage,
    _decimal,
    _normalized,
    _organization_value,
    project_criteria,
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


def _reasons_from_factors(
    factors: list[FactorContribution],
    listing: Any,
    organization: Any,
    criteria: JobCriteria,
) -> list[str]:
    """Translate scoring factors into concise human-readable reasons."""
    reasons: list[str] = []

    # Public company reason
    funding = _organization_value(listing, organization, "funding_round")
    if funding == "P":
        reasons.append("Public company")
    elif funding and criteria.require_public_company is not True:
        reasons.append(_funding_label(funding))

    # RTO / work location reason
    rto = _organization_value(listing, organization, "rto_policy")
    if rto:
        rto_text = _rto_label(rto)
        if rto == "R":
            reasons.append("Remote")
        elif rto == "H":
            days = _RTO_DAYS.get(rto, 3)
            reasons.append(f"Hybrid (≤{days} days)")
        else:
            reasons.append(rto_text)

    # Compensation reason
    for factor in factors:
        if factor.factor == "compensation" and factor.score > 0:
            comp_min = getattr(listing, "compensation_min", None)
            comp_max = getattr(listing, "compensation_max", None)
            if comp_min is not None:
                reasons.append(f"Salary {comp_min:,}+")
            elif comp_max is not None:
                reasons.append(f"Salary up to {comp_max:,}")
            break

    # Organization score reason
    for factor in factors:
        if factor.factor == "organization_scores" and factor.score > 0:
            # Extract average from detail string
            detail = factor.detail
            if "average=" in detail:
                avg_str = detail.split("average=")[1].split("/")[0]
                try:
                    avg_val = float(avg_str)
                    reasons.append(f"Score {avg_val:.1f}")
                except (ValueError, TypeError):
                    pass
            break

    # Industry match reason
    for factor in factors:
        if factor.factor == "industry" and "matched=" in factor.detail:
            matched = factor.detail.split("matched=")[1]
            if matched and matched != "none":
                reasons.append(f"Industry: {matched}")
            break

    # Vesting reason
    for factor in factors:
        if factor.factor == "vesting" and factor.score > 0:
            reasons.append("Vesting aligns")
            break

    # Culture reason
    for factor in factors:
        if factor.factor == "culture" and "matched=" in factor.detail:
            matched = factor.detail.split("matched=")[1]
            if matched and matched != "none":
                reasons.append(f"Culture: {matched}")
            break

    # Freshness reason
    last_seen = getattr(listing, "last_seen_at", None)
    if last_seen is not None:
        reasons.append("Recent listing")

    return reasons[:6]  # Bound to 6 reasons max


def _reasons_for_org(
    organization: Any,
    criteria: JobCriteria,
    score: float,
) -> list[str]:
    """Generate human-readable reasons for an organization match."""
    reasons: list[str] = []
    funding = getattr(organization, "funding_round", None)
    if funding == "P":
        reasons.append("Public company")
    elif funding:
        reasons.append(_funding_label(funding))

    rto = getattr(organization, "rto_policy", None)
    if rto:
        rto_text = _rto_label(rto)
        if rto == "R":
            reasons.append("Remote")
        elif rto == "H":
            days = _RTO_DAYS.get(rto, 3)
            reasons.append(f"Hybrid (≤{days} days)")
        else:
            reasons.append(rto_text)

    # Score reason
    try:
        scores = organization.avg_scores()
        values: list[float] = []
        for row in scores or ():
            if isinstance(row, dict):
                val = _decimal(row.get("avg_score", row.get("score")))
                if val is not None:
                    values.append(float(val))
        if values:
            avg = sum(values) / len(values)
            reasons.append(f"Score {avg:.1f}")
    except (AttributeError, TypeError, ValueError):
        pass

    # Industry match
    org_industries = _normalized(getattr(organization, "industry", "")).split()
    if org_industries and criteria.industries:
        matched = [ind for ind in org_industries if ind in criteria.industries]
        if matched:
            reasons.append(f"Industry: {', '.join(matched[:3])}")

    # Vesting
    if getattr(organization, "accelerated_vesting", False) and \
            criteria.prefer_accelerated:
        reasons.append("Accelerated vesting")

    return reasons[:6]


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


def _org_excluded(organization: Any, criteria: JobCriteria) -> bool:
    """Check if an organization is excluded by hard filters."""
    name = _normalized(getattr(organization, "name", ""))
    if name and name in criteria.excluded_companies:
        return True
    industry = _strings_safe(getattr(organization, "industry", ""))
    if industry and industry & criteria.excluded_industries:
        return True
    if criteria.require_public_company is True:
        funding = getattr(organization, "funding_round", None)
        if funding and funding != "P":
            return True
    if criteria.max_in_office_days is not None:
        rto = getattr(organization, "rto_policy", None)
        assumed_days = _RTO_DAYS.get(rto) if rto else None
        if assumed_days is not None and assumed_days > criteria.max_in_office_days:
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _get_criteria(user) -> JobCriteria | None:
    """Read the user's preference document and project it to JobCriteria."""
    try:
        pref = UserPreference.objects.get(user=user)
    except UserPreference.DoesNotExist:
        return None
    return project_criteria(pref.preferences, pref.schema_version)


def match_jobs(
    user,
    *,
    limit: int = MAX_MATCH_RESULTS,
    config: RankingConfig = DEFAULT_CONFIG,
    queryset: Any = None,
) -> list[JobMatchResult]:
    """Return ranked job-listing matches for *user*.

    1. Read preferences → JobCriteria.
    2. Query active job listings with organizations.
    3. Hard-filter (public-only, RTO ceiling, exclusions).
    4. Rank survivors with the deterministic engine.
    5. Attach human-readable reasons.
    6. Return a bounded, stable-ordered list.
    """
    criteria = _get_criteria(user)
    if criteria is None:
        return []

    capped = max(1, min(limit, MAX_MATCH_RESULTS))
    if queryset is None:
        queryset = JobListing.objects.select_related("organization").filter(
            status=JobListing.Status.ACTIVE
        )
    listings = list(queryset[:capped * 4])  # over-fetch before filtering

    ranked = rank_listings_with_reasons(listings, criteria, config)
    return ranked[:capped]


def match_organizations(
    user,
    *,
    limit: int = MAX_MATCH_RESULTS,
    config: RankingConfig = DEFAULT_CONFIG,
    queryset: Any = None,
) -> list[OrgMatchResult]:
    """Return ranked organization matches for *user*.

    1. Read preferences → JobCriteria.
    2. Query active, public organizations.
    3. Hard-filter (public-only, RTO ceiling, exclusions).
    4. Score survivors.
    5. Attach human-readable reasons.
    6. Return a bounded, stable-ordered list.
    """
    criteria = _get_criteria(user)
    if criteria is None:
        return []

    capped = max(1, min(limit, MAX_MATCH_RESULTS))
    if queryset is None:
        queryset = Organization.objects.filter(status=1, public=True)
    orgs = list(queryset[:capped * 4])

    survivors = [
        org for org in orgs if not _org_excluded(org, criteria)
    ]

    scored: list[tuple[float, int, OrgMatchResult]] = []
    for org in survivors:
        score = _score_organization_match(org, criteria, config)
        reasons = _reasons_for_org(org, criteria, score)
        result = OrgMatchResult(
            organization_id=int(org.pk),
            name=str(org.name),
            url=str(getattr(org, "url", "")),
            funding_round=str(getattr(org, "funding_round", "")),
            rto_policy=str(getattr(org, "rto_policy", "")),
            score=score,
            reasons=reasons,
        )
        scored.append((score, int(org.pk), result))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:capped]]


def rank_listings_with_reasons(
    listings: Iterable[Any],
    criteria: JobCriteria,
    config: RankingConfig = DEFAULT_CONFIG,
) -> list[JobMatchResult]:
    """Rank listings and produce JobMatchResult objects with reasons.

    This is the shared ranking+reason engine used by both the API and chat tool.
    """
    from crank.agents.jobs.matching import rank_listings

    ranked: list[MatchResult] = rank_listings(listings, criteria, config)
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
        reasons = _reasons_from_factors(
            match.factors, listing, organization, criteria
        )
        org_id = (
            int(organization.pk)
            if organization and getattr(organization, "pk", None)
            else None
        )
        org_name = str(getattr(organization, "name", "")) if organization else ""
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
        ))
    return results


__all__ = [
    "MAX_MATCH_RESULTS",
    "JobMatchResult",
    "OrgMatchResult",
    "match_jobs",
    "match_organizations",
    "rank_listings_with_reasons",
]
