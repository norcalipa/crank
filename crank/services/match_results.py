# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Committed-generation reads for job matches (issue #475).

Gated by :func:`read_enabled` (``MATCH_RESULTS_READ_ENABLED`` plus the
``match_results_read`` ``CapabilitySwitch``). Every caller — the ranked
endpoint (via ``crank.services.job_matching.match_jobs``), the list/detail/
status views, and the assistant's ``get_matches_for_user`` — reads through
:func:`current_generation_queryset` / :func:`current_job_results`, so they
see one identical ``revision`` block. When no committed generation exists
yet (or the gate is off), callers fall back to their existing live path
unchanged.
"""
from __future__ import annotations

from django.conf import settings
from django.db.models import Subquery

from crank.agents.jobs.matching import (
    _decimal,
    coverage,
    outcomes_from_dicts,
    reasons_from_requirements,
)
from crank.models.job import JobListing
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.preference import UserPreference
from crank.services.preferences import unsupported_criteria


def read_enabled():
    """Whether committed-generation reads may serve the page/assistant.

    Gated by ``MATCH_RESULTS_READ_ENABLED`` (default False) plus the
    ``match_results_read`` ``CapabilitySwitch`` (absent switch defaults to
    enabled once the settings flag is on).
    """
    from crank.services import monitoring

    if not getattr(settings, "MATCH_RESULTS_READ_ENABLED", False):
        return False
    return monitoring.capability_enabled("match_results_read", default=True)


def _company_score_of(organization):
    """The organization's average score (0-5), or ``None`` when unknown."""
    if organization is None:
        return None
    try:
        scores = organization.avg_scores()
    except (AttributeError, TypeError, ValueError):
        return None
    values = []
    for row in scores or ():
        if not isinstance(row, dict):
            continue
        value = _decimal(row.get("avg_score", row.get("score")))
        if value is not None:
            values.append(float(value))
    if not values:
        return None
    return max(0.0, min(5.0, sum(values) / len(values)))


def current_generation_queryset(user):
    """The current, committed generation's rows for *user* — one statement.

    A subquery on the state's ``current_generation`` makes this consistent
    under READ COMMITTED or REPEATABLE READ without extra locks. Returns
    ``none()`` when the user has no ``UserPreference`` (preserving today's
    "no preferences -> no matches" behavior) or has no committed generation
    yet. Excludes dismissed rows and inactive listings.
    """
    if user is None or getattr(user, "pk", None) is None:
        return JobMatch.objects.none()
    if not UserPreference.objects.filter(user=user).exists():
        return JobMatch.objects.none()
    current_generation = MatchResultState.objects.filter(user=user).values(
        "current_generation"
    )[:1]
    return (
        JobMatch.objects.filter(
            user=user,
            dismissed=False,
            listing__status=JobListing.Status.ACTIVE,
            result_generation=Subquery(current_generation),
        )
        .select_related("listing", "organization")
    )


def current_match_count(user):
    """Count of the user's current, non-dismissed, active-listing matches."""
    return current_generation_queryset(user).count()


def revision_block(state, *, stale):
    """The additive ``revision`` block shared by every committed-read row."""
    from crank.services import match_recompute

    generated_at = state.generated_at if state else None
    return {
        "preference_revision": state.preference_revision if state else None,
        "ranking_version": state.ranker_version if state else "",
        "data_revision": state.data_revision if state else None,
        "generated_at": generated_at.isoformat() if generated_at else None,
        "stale": stale,
        "result_generation": state.current_generation if state else None,
        "pending": bool(stale and match_recompute.recompute_enabled()),
    }


def current_job_results(user, limit):
    """Committed-generation :class:`JobMatchResult` rows, or ``[]`` when none.

    Reasons are rendered from the stored ``requirements`` through the same
    shared renderer as the persisted-match view
    (``crank.agents.jobs.matching.outcomes_from_dicts``).
    """
    from crank.services.job_matching import JobMatchResult

    state = MatchResultState.objects.filter(user=user).first()
    if state is None or state.current_generation is None:
        return []
    pref = UserPreference.objects.filter(user=user).first()
    if pref is None:
        return []
    unsupported = unsupported_criteria(pref.preferences)
    stale = (
        state.preference_revision is not None
        and pref.revision is not None
        and state.preference_revision < pref.revision
    )
    revision = revision_block(state, stale=stale)
    rows = list(
        current_generation_queryset(user).order_by("-score", "id")[: max(1, int(limit))]
    )
    results = []
    for match in rows:
        outcomes = outcomes_from_dicts(match.requirements)
        listing = match.listing
        organization = match.organization
        results.append(
            JobMatchResult(
                listing_id=listing.pk,
                title=listing.title,
                employer_name=listing.employer_name,
                organization_id=organization.pk if organization else None,
                organization_name=organization.name if organization else "",
                canonical_url=listing.canonical_url,
                location_text=listing.location_text,
                is_remote=listing.is_remote,
                score=match.score,
                reasons=reasons_from_requirements(outcomes),
                factors=match.factors,
                fit_score=match.score,
                company_score=_company_score_of(organization),
                coverage=coverage(outcomes),
                requirements=[outcome.as_dict() for outcome in outcomes],
                unsupported=list(unsupported),
                evidence_ids=list(match.evidence_ids or []),
                preference_revision=revision["preference_revision"],
                ranking_version=revision["ranking_version"],
                data_revision=revision["data_revision"],
                generated_at=match.generated_at,
                stale=stale,
                result_generation=match.result_generation,
                pending=revision["pending"],
            )
        )
    return results


__all__ = [
    "current_generation_queryset",
    "current_job_results",
    "current_match_count",
    "read_enabled",
    "revision_block",
]
