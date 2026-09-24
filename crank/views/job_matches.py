# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Authenticated JSON endpoints for owner-scoped job matches."""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from crank.agents.jobs.match_persist import ensure_match_result_state
from crank.agents.jobs.matching import (
    coverage,
    outcomes_from_dicts,
    reasons_from_requirements,
)
from crank.empty_state import NO_MATCHES, derive_state
from crank.models.job import JobListing
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.preference import UserPreference
from crank.services import match_recompute, match_results
from crank.services.job_matching import (
    MAX_MATCH_RESULTS,
    match_jobs,
    match_organizations,
    relaxation_preview,
)
from crank.services.preferences import unsupported_criteria

_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100


def _organization_payload(organization):
    if organization is None:
        return None
    return {"id": organization.pk, "name": organization.name}


def _listing_payload(listing):
    return {
        "id": listing.pk,
        "title": listing.title,
        "employer_name": listing.employer_name,
        "canonical_url": listing.canonical_url,
        "location_text": listing.location_text,
        "is_remote": listing.is_remote,
        "compensation_min": listing.compensation_min,
        "compensation_max": listing.compensation_max,
        "compensation_currency": listing.compensation_currency,
        "compensation_interval": listing.compensation_interval,
        "description_excerpt": listing.description_excerpt,
        "status": listing.status,
        "organization": _organization_payload(listing.organization),
    }


# issue #475: the single shared implementation lives in match_results.py so
# the committed-generation reads and this view compute company_score
# identically.
_company_score_of = match_results._company_score_of


def _preference_meta(user):
    """Current document revision and unsupported criteria for *user*."""
    if user is None or getattr(user, "pk", None) is None:
        return None, []
    pref = UserPreference.objects.filter(user=user).first()
    if pref is None:
        return None, []
    return pref.revision, unsupported_criteria(pref.preferences)


def _reasons_from_stored_factors(requirements):
    """Render reasons from the stored requirement structure (single renderer)."""
    outcomes = outcomes_from_dicts(requirements)
    return reasons_from_requirements(outcomes)


def _match_payload(match, *, detail=False, user=None, revision=None):
    """``revision`` overrides the per-row calculation with the shared,
    generation-level block (issue #475 review, AC4): pass it whenever the
    match came from ``match_results.load_current`` so list/detail responses
    carry the identical ``revision`` block the ranked and assistant surfaces
    use, instead of recomputing from this one row's stamped fields."""
    current_revision, unsupported = _preference_meta(user)
    outcomes = outcomes_from_dicts(match.requirements)
    if revision is None:
        stale = (
            match.preference_revision is not None
            and current_revision is not None
            and match.preference_revision < current_revision
        )
        revision = {
            "preference_revision": match.preference_revision,
            "ranking_version": match.ranker_version,
            "data_revision": match.data_revision,
            "generated_at": (
                match.generated_at.isoformat() if match.generated_at else None
            ),
            "stale": stale,
            # issue #475: additive committed-generation fields.
            "result_generation": match.result_generation,
            "pending": bool(stale and match_recompute.recompute_enabled()),
        }
    payload = {
        "id": match.pk,
        "listing": _listing_payload(match.listing),
        "organization": _organization_payload(match.organization),
        "preference_version": match.preference_version,
        "ranker_version": match.ranker_version,
        "score": match.score,
        "fit_score": match.score,
        "company_score": _company_score_of(match.organization),
        "coverage": coverage(outcomes),
        "requirements": [o.as_dict() for o in outcomes],
        "unsupported": list(unsupported),
        "evidence_ids": list(match.evidence_ids or []),
        "revision": revision,
        "reasons": reasons_from_requirements(outcomes),
        "first_matched_at": match.first_matched_at,
        "last_matched_at": match.last_matched_at,
        "seen_at": match.seen_at,
        "dismissed": match.dismissed,
    }
    if detail:
        payload["factors"] = match.factors
    return payload


def _parse_page(request):
    try:
        page_number = int(request.GET.get("page", 1))
        page_size = int(request.GET.get("page_size", _DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        return None, None
    if page_number < 1 or page_size < 1:
        return None, None
    return page_number, min(page_size, _MAX_PAGE_SIZE)


def _pagination_response(page, user=None, revision=None):
    return {
        "count": page.paginator.count,
        "next": page.next_page_number() if page.has_next() else None,
        "previous": page.previous_page_number() if page.has_previous() else None,
        "results": [
            _match_payload(match, user=user, revision=revision)
            for match in page.object_list
        ],
    }


def _preference_exists(user):
    return UserPreference.objects.filter(user=user).exists()


def _reads_context(user):
    """``(rows, shared_revision_or_None)`` for list/status reads.

    ``rows`` is a live ``QuerySet`` in the fallback case, or a materialized
    ``list`` when read from a committed generation (issue #475 review round
    2: :func:`match_results.load_current` must materialize the rows itself
    to keep them consistent with the generation they're read against — see
    its docstring — so this must not re-wrap the result in a fresh,
    unpinned queryset via a further ``.order_by()``, which would reopen
    that race).

    Deleting a preference must hide stored matches in every read (owner
    decision, issue #475 review finding 4), with no physical deletion of the
    ``JobMatch``/``MatchResultState`` rows — so a missing ``UserPreference``
    always yields ``none()``, live fallback included. When a committed
    generation exists and the gate is on, ``state`` and ``rows`` come from
    one :func:`match_results.load_current` read, so the returned
    ``revision`` block matches the rows exactly (finding 6) and is identical
    to the block the ranked/assistant surfaces build from the same state
    (finding 5, AC4).
    """
    if not _preference_exists(user):
        return JobMatch.objects.none(), None
    live = (
        JobMatch.objects.filter(
            user=user,
            dismissed=False,
            listing__status=JobListing.Status.ACTIVE,
        )
        .select_related("listing", "organization")
        .order_by("-score", "id")
    )
    if not match_results.read_enabled():
        return live, None
    state, rows = match_results.load_current(user)
    if state is None:
        return live, None
    pref = UserPreference.objects.filter(user=user).first()
    stale = match_results.is_stale(state, pref)
    revision = match_results.revision_block(state, stale=stale)
    return rows, revision


@login_required
@require_GET
def job_match_list(request):
    """Return the authenticated user's active, non-dismissed matches."""
    page_number, page_size = _parse_page(request)
    if page_number is None:
        return JsonResponse(
            {"error": "page and page_size must be positive integers."}, status=400
        )
    queryset, revision = _reads_context(request.user)
    paginator = Paginator(queryset, page_size)
    page = paginator.get_page(page_number)
    return JsonResponse(_pagination_response(page, request.user, revision=revision))


@login_required
@require_GET
def job_match_detail(request, match_id):
    """Return one active match owned by the authenticated user.

    404s when the user's preferences were deleted (issue #475 review
    finding 4): stored matches must not stay visible after that, even
    though the row itself is never physically deleted.
    """
    if not _preference_exists(request.user):
        raise Http404
    match = get_object_or_404(
        JobMatch.objects.select_related("listing", "organization"),
        pk=match_id,
        user=request.user,
        listing__status=JobListing.Status.ACTIVE,
    )
    return JsonResponse(_match_payload(match, detail=True, user=request.user))


@login_required
@require_POST
def job_match_seen(request, match_id):
    """Mark an owned match as seen and return its updated representation.

    Updates every version-row for ``(user, listing)`` under the
    ``MatchResultState`` lock (issue #475 review finding 3, plan §4.2
    lifecycle/§5.3): without that lock, a concurrent version-bump publish
    can ``bulk_create`` a new version-key row for the same listing that
    never carries this request's ``seen_at``, silently losing it. Locking
    the same state row :func:`crank.agents.jobs.match_persist.publish` uses
    serializes the two.
    """
    match = get_object_or_404(
        JobMatch.objects.select_related("listing", "organization"),
        pk=match_id,
        user=request.user,
        listing__status=JobListing.Status.ACTIVE,
    )
    # Always take the lock and run the idempotent all-version update, even
    # when the requested row already looks seen (issue #475 review round 2,
    # MINOR finding 3): gating on `match.seen_at is None` skipped the repair
    # for version rows that already disagree — e.g. a version-bump publish
    # `bulk_create`d a new-key row before this request's earlier attempt
    # locked, leaving that row unseen while the requested one is seen.
    # Posting the already-seen row would then never repair it.
    state = ensure_match_result_state(request.user)
    now = timezone.now()
    with transaction.atomic():
        MatchResultState.objects.select_for_update().filter(pk=state.pk).first()
        JobMatch.objects.filter(
            user=request.user, listing_id=match.listing_id, seen_at__isnull=True
        ).update(seen_at=now, modified=now)
    match.refresh_from_db()
    return JsonResponse(_match_payload(match, detail=True, user=request.user))


@login_required
@require_POST
def job_match_dismiss(request, match_id):
    """Dismiss an owned match and return its updated representation.

    Updates every version-row for ``(user, listing)`` under the
    ``MatchResultState`` lock, for the same reason as :func:`job_match_seen`.
    """
    match = get_object_or_404(
        JobMatch.objects.select_related("listing", "organization"),
        pk=match_id,
        user=request.user,
        listing__status=JobListing.Status.ACTIVE,
    )
    # Always take the lock and run the idempotent all-version update, even
    # when the requested row already looks dismissed (issue #475 review
    # round 2, MINOR finding 3) — see the matching comment in
    # ``job_match_seen`` above.
    state = ensure_match_result_state(request.user)
    now = timezone.now()
    with transaction.atomic():
        MatchResultState.objects.select_for_update().filter(pk=state.pk).first()
        JobMatch.objects.filter(
            user=request.user, listing_id=match.listing_id, dismissed=False
        ).update(dismissed=True, modified=now)
    match.refresh_from_db()
    return JsonResponse(_match_payload(match, detail=True, user=request.user))


def _probe_cap() -> int:
    """Bounded relaxation-probe cap from settings; misconfiguration fails safe."""
    try:
        return max(0, int(getattr(settings, "JOB_MATCH_RELAXATION_PROBES", 3)))
    except (TypeError, ValueError):
        return 3


def _status_count(user):
    """Match count for the polled status endpoint: a single ``COUNT``.

    Uses the committed generation when reads are enabled and one exists
    (one statement, pinned by a subquery on the state row), else the live
    filter. A missing preference always counts zero (owner decision).
    """
    if not _preference_exists(user):
        return 0
    if match_results.read_enabled() and MatchResultState.objects.filter(
        user=user, current_generation__isnull=False
    ).exists():
        return match_results.current_match_count(user)
    return JobMatch.objects.filter(
        user=user, dismissed=False, listing__status=JobListing.Status.ACTIVE
    ).count()


@login_required
@require_GET
def job_match_status(request):
    """Return the current inventory/match state for the authenticated user.

    This endpoint powers the empty-state UI. It derives a single canonical
    ``EmptyState`` so the chat and job-match surfaces use the same wording.
    Staff-only details (crawl error summaries, internal state names) are
    included only when the requester is a staff member.

    Read-only by contract: neither this view nor the zero-match relaxation
    preview ever writes preferences. Additive payload fields (``refreshing``,
    ``coverage``, ``active_constraints``, ``inventory``,
    ``relaxation_preview``) are emitted only when meaningful.
    """
    match_count = _status_count(request.user)
    state = derive_state(user=request.user, match_count=match_count)
    is_staff = bool(request.user.is_staff)
    payload = state.to_dict(include_staff=is_staff)
    if state.state == NO_MATCHES:
        payload["relaxation_preview"] = relaxation_preview(
            request.user,
            max_probes=_probe_cap(),
            limit=MAX_MATCH_RESULTS,
        )
    return JsonResponse(payload)


@login_required
@require_GET
def job_match_ranked(request):
    """Return live preference-grounded ranked matches for the authenticated user.

    This endpoint computes matches on-the-fly from the user's saved preferences,
    applying hard filters and deterministic scoring. Returns job matches and
    organization matches with human-readable reason strings.
    """
    try:
        limit = int(request.GET.get("limit", MAX_MATCH_RESULTS))
    except (TypeError, ValueError):
        limit = MAX_MATCH_RESULTS
    limit = max(1, min(limit, MAX_MATCH_RESULTS))

    job_results = match_jobs(request.user, limit=limit)
    org_results = match_organizations(request.user, limit=limit)

    return JsonResponse({
        "job_matches": [
            {
                "listing_id": r.listing_id,
                "title": r.title,
                "employer_name": r.employer_name,
                "organization_id": r.organization_id,
                "organization_name": r.organization_name,
                "canonical_url": r.canonical_url,
                "location_text": r.location_text,
                "is_remote": r.is_remote,
                "score": r.score,
                "fit_score": r.fit_score,
                "company_score": r.company_score,
                "coverage": r.coverage,
                "requirements": r.requirements,
                "unsupported": r.unsupported,
                "evidence_ids": r.evidence_ids,
                "revision": r.revision(),
                "reasons": r.reasons,
                "factors": r.factors,
            }
            for r in job_results
        ],
        "organization_matches": [
            {
                "organization_id": r.organization_id,
                "name": r.name,
                "url": r.url,
                "funding_round": r.funding_round,
                "rto_policy": r.rto_policy,
                "score": r.score,
                "fit_score": r.fit_score,
                "company_score": r.company_score,
                "coverage": r.coverage,
                "requirements": r.requirements,
                "unsupported": r.unsupported,
                "evidence_ids": r.evidence_ids,
                "revision": r.revision(),
                "reasons": r.reasons,
            }
            for r in org_results
        ],
    })


__all__ = [
    "job_match_detail",
    "job_match_dismiss",
    "job_match_list",
    "job_match_ranked",
    "job_match_seen",
    "job_match_status",
]
