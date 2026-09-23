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

import logging

from django.conf import settings
from django.db.models import Subquery

from crank.agents.jobs.matching import (
    _decimal,
    coverage,
    outcomes_from_dicts,
    reasons_from_requirements,
)
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG
from crank.models.job import JobListing
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.preference import UserPreference
from crank.services.preferences import unsupported_criteria

logger = logging.getLogger("match_results")

# Bounded retries for load_current's materialize-then-revalidate loop
# (issue #475 review round 2). A publish concurrent with the retry window
# is rare in practice; this bound just prevents pathological live-lock
# under sustained contention.
_LOAD_CURRENT_MAX_ATTEMPTS = 3


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


def current_generation_queryset(user, *, generation=None):
    """The current, committed generation's rows for *user* — one statement.

    ``generation`` pins the row filter to an already-read
    ``MatchResultState.current_generation`` value (issue #475 review): when
    the caller already fetched the state row for its metadata, reusing that
    same value here — instead of a fresh subquery — keeps the rows and the
    metadata describing them from two different generations if a publish
    commits in between. When ``generation`` is omitted, a subquery on the
    state's ``current_generation`` makes this consistent under READ
    COMMITTED or REPEATABLE READ without extra locks (used when there is no
    already-read state to pin to). Returns ``none()`` when the user has no
    ``UserPreference`` (preserving today's "no preferences -> no matches"
    behavior) or has no committed generation yet. Excludes dismissed rows
    and inactive listings.
    """
    if user is None or getattr(user, "pk", None) is None:
        return JobMatch.objects.none()
    if not UserPreference.objects.filter(user=user).exists():
        return JobMatch.objects.none()
    if generation is not None:
        result_generation = generation
    else:
        result_generation = Subquery(
            MatchResultState.objects.filter(user=user).values("current_generation")[:1]
        )
    return (
        JobMatch.objects.filter(
            user=user,
            dismissed=False,
            listing__status=JobListing.Status.ACTIVE,
            result_generation=result_generation,
        )
        .select_related("listing", "organization")
    )


def current_match_count(user):
    """Count of the user's current, non-dismissed, active-listing matches."""
    return current_generation_queryset(user).count()


def load_current(user, *, order_by=("-score", "id"), limit=None):
    """Read the state row and its exact-generation rows as one consistent
    snapshot, materialized immediately.

    Returns ``(state, rows)``: ``state`` is ``None`` when the user has no
    ``UserPreference`` or no committed generation yet, in which case
    ``rows`` is ``[]``. Otherwise ``rows`` is a materialized ``list`` of
    ``JobMatch`` pinned to ``state.current_generation``.

    Pinning the row filter to an already-read generation value is not
    enough on its own (issue #475 review round 2): ``publish()`` moves
    *continuing* matches onto the new generation in place (``bulk_update``
    sets their ``result_generation`` to the new ticket), so a publish that
    commits between reading ``state`` and evaluating a **lazy** queryset
    pinned to the old generation would make an established generation look
    empty or partial by the time the caller (e.g. pagination) evaluates it.
    To close that window, this materializes the rows immediately and then
    re-reads ``current_generation``; if it has moved on, the whole read is
    retried (bounded) against the new generation, so the returned ``state``
    and ``rows`` are always a genuinely consistent pair, list count and page
    included, with no separate row-count query needed downstream.
    """
    if not UserPreference.objects.filter(user=user).exists():
        return None, []
    state = None
    rows = []
    for attempt in range(_LOAD_CURRENT_MAX_ATTEMPTS):
        state = MatchResultState.objects.filter(
            user=user, current_generation__isnull=False
        ).first()
        if state is None:
            return None, []
        generation = state.current_generation
        qs = current_generation_queryset(user, generation=generation)
        if order_by:
            qs = qs.order_by(*order_by)
        if limit is not None:
            qs = qs[: max(1, int(limit))]
        rows = list(qs)
        still_current = (
            MatchResultState.objects.filter(user=user)
            .values_list("current_generation", flat=True)
            .first()
        )
        if still_current == generation:
            return state, rows
        if attempt + 1 < _LOAD_CURRENT_MAX_ATTEMPTS:
            logger.info(
                "load_current: generation advanced from %s during read "
                "(user_id=%s, attempt=%s); retrying",
                generation,
                getattr(user, "pk", user),
                attempt,
            )
    # Retries exhausted under sustained concurrent publishing: return the
    # last read snapshot. state and rows are still a matched pair (both
    # came from the same iteration), just possibly already superseded.
    return state, rows


def _tag_mismatch(current_value, stamped_value):
    """Whether a generation's stamped tag disagrees with the current value
    (issue #475 review round 2, MINOR finding 2): a *greater* stamped value
    (shouldn't happen, but would previously read as "current") and a
    *missing* stamped tag (a pre-#475 generation, or one that raced a
    concurrent publish before the tag was set) both count as a mismatch,
    same as ``pending_users``/``open_snapshot``'s dirtiness check — not just
    a strictly older, non-null one. ``current_value is None`` means there is
    nothing to compare against, so it is never a mismatch on its own.
    """
    if current_value is None:
        return False
    return stamped_value != current_value


def is_stale(state, pref, *, ranker_version=None):
    """Whether *state* (a committed generation) is behind *pref*'s current
    document or the current ranker (issue #475 review, plan §5.3): a
    preference-revision, schema-version, or ranker-version mismatch all
    count as stale — any inequality against the current tag, not only an
    older non-null revision (round-2 review finding 2)."""
    if state is None or pref is None:
        return False
    if ranker_version is None:
        ranker_version = DEFAULT_CONFIG.version
    if _tag_mismatch(pref.revision, state.preference_revision):
        return True
    if _tag_mismatch(pref.schema_version, state.preference_version):
        return True
    if _tag_mismatch(ranker_version, state.ranker_version):
        return True
    return False


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

    pref = UserPreference.objects.filter(user=user).first()
    if pref is None:
        return []
    state, rows = load_current(user, limit=limit)
    if state is None:
        return []
    unsupported = unsupported_criteria(pref.preferences)
    stale = is_stale(state, pref)
    revision = revision_block(state, stale=stale)
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
    "is_stale",
    "load_current",
    "read_enabled",
    "revision_block",
]
