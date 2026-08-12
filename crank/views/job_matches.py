# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Authenticated JSON endpoints for owner-scoped job matches."""

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from crank.models.job import JobListing
from crank.models.job_match import JobMatch

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


__all__ = [
    "job_match_list",
    "job_match_detail",
    "job_match_seen",
    "job_match_dismiss",
]
