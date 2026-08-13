# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Authenticated JSON endpoints for owner-scoped job matches."""

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from crank.empty_state import derive_state
from crank.models.job import JobListing
from crank.models.job_match import JobMatch
from crank.services.job_matching import (
    MAX_MATCH_RESULTS,
    match_jobs,
    match_organizations,
)

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


def _match_payload(match, *, detail=False):
    payload = {
        "id": match.pk,
        "listing": _listing_payload(match.listing),
        "organization": _organization_payload(match.organization),
        "preference_version": match.preference_version,
        "ranker_version": match.ranker_version,
        "score": match.score,
        "reasons": _reasons_from_stored_factors(match.factors),
        "first_matched_at": match.first_matched_at,
        "last_matched_at": match.last_matched_at,
        "seen_at": match.seen_at,
        "dismissed": match.dismissed,
    }
    if detail:
        payload["factors"] = match.factors
    return payload


def _reasons_from_stored_factors(factors):
    """Translate stored factor dicts into concise human-readable reasons."""
    reasons = []
    if not factors or not isinstance(factors, list):
        return reasons
    for factor in factors:
        if not isinstance(factor, dict):
            continue
        name = factor.get("factor", "")
        score = factor.get("score", 0)
        detail = factor.get("detail", "")
        if name == "organization_scores" and score > 0 and "average=" in detail:
            avg_str = detail.split("average=")[1].split("/")[0]
            try:
                reasons.append(f"Score {float(avg_str):.1f}")
            except (ValueError, TypeError):
                pass
        elif name == "compensation" and score > 0:
            reasons.append("Compensation match")
        elif name == "work_location" and score > 0:
            reasons.append("Location match")
        elif name == "vesting" and score > 0:
            reasons.append("Vesting aligns")
        elif name == "culture" and "matched=" in detail:
            matched = detail.split("matched=")[1]
            if matched and matched != "none":
                reasons.append(f"Culture: {matched}")
        elif name == "industry" and "matched=" in detail:
            matched = detail.split("matched=")[1]
            if matched and matched != "none":
                reasons.append(f"Industry: {matched}")
    return reasons[:6]


def _parse_page(request):
    try:
        page_number = int(request.GET.get("page", 1))
        page_size = int(request.GET.get("page_size", _DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        return None, None
    if page_number < 1 or page_size < 1:
        return None, None
    return page_number, min(page_size, _MAX_PAGE_SIZE)


def _pagination_response(page):
    return {
        "count": page.paginator.count,
        "next": page.next_page_number() if page.has_next() else None,
        "previous": page.previous_page_number() if page.has_previous() else None,
        "results": [_match_payload(match) for match in page.object_list],
    }


@login_required
@require_GET
def job_match_list(request):
    """Return the authenticated user's active, non-dismissed matches."""
    page_number, page_size = _parse_page(request)
    if page_number is None:
        return JsonResponse(
            {"error": "page and page_size must be positive integers."}, status=400
        )
    queryset = (
        JobMatch.objects.filter(
            user=request.user,
            dismissed=False,
            listing__status=JobListing.Status.ACTIVE,
        )
        .select_related("listing", "organization")
        .order_by("-score", "id")
    )
    paginator = Paginator(queryset, page_size)
    page = paginator.get_page(page_number)
    return JsonResponse(_pagination_response(page))


@login_required
@require_GET
def job_match_detail(request, match_id):
    """Return one active match owned by the authenticated user."""
    match = get_object_or_404(
        JobMatch.objects.select_related("listing", "organization"),
        pk=match_id,
        user=request.user,
        listing__status=JobListing.Status.ACTIVE,
    )
    return JsonResponse(_match_payload(match, detail=True))


@login_required
@require_POST
def job_match_seen(request, match_id):
    """Mark an owned match as seen and return its updated representation."""
    match = get_object_or_404(
        JobMatch.objects.select_related("listing", "organization"),
        pk=match_id,
        user=request.user,
        listing__status=JobListing.Status.ACTIVE,
    )
    if match.seen_at is None:
        match.seen_at = timezone.now()
        match.save(update_fields=["seen_at", "modified"])
    return JsonResponse(_match_payload(match, detail=True))


@login_required
@require_POST
def job_match_dismiss(request, match_id):
    """Dismiss an owned match and return its updated representation."""
    match = get_object_or_404(
        JobMatch.objects.select_related("listing", "organization"),
        pk=match_id,
        user=request.user,
        listing__status=JobListing.Status.ACTIVE,
    )
    if not match.dismissed:
        match.dismissed = True
        match.save(update_fields=["dismissed", "modified"])
    return JsonResponse(_match_payload(match, detail=True))


@login_required
@require_GET
def job_match_status(request):
    """Return the current inventory/match state for the authenticated user.

    This endpoint powers the empty-state UI. It derives a single canonical
    ``EmptyState`` so the chat and job-match surfaces use the same wording.
    Staff-only details (crawl error summaries, internal state names) are
    included only when the requester is a staff member.
    """
    match_count = (
        JobMatch.objects.filter(
            user=request.user,
            dismissed=False,
            listing__status=JobListing.Status.ACTIVE,
        ).count()
    )
    state = derive_state(user=request.user, match_count=match_count)
    is_staff = bool(request.user.is_staff)
    return JsonResponse(state.to_dict(include_staff=is_staff))


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
