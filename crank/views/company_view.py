# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Session-only reads for the company view (issue #486).

Two read-only endpoints that answer for the signed-in reader alone: how one
company fits that reader's saved priorities, and the open jobs at that
company. Neither writes anything, records telemetry, or fills the shared
per-organization cache entries the API views use; the one entry a fit read
fills is the reader-neutral average-score entry that ``Organization.avg_scores``
keeps for every reader. Every answer is ``private, no-store``.

Which organizations get a page is the same rule the three organization
endpoints in ``crank/views/api.py`` use (active records, 404 otherwise); the
``public`` flag is deliberately not consulted here (see issue #561).
"""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from crank.models.job import JobListing, JobSourceCatalog
from crank.models.organization import Organization
from crank.services.job_matching import organization_fit
from crank.views.job_matches import _listing_payload

#: The sources whose listings count, the filter every other reader of job
#: inventory uses (``match_inventory``, the assistant status, inventory health):
#: a blocked, pending or disabled source never contributes.
LIVE_SOURCE = {"approval_state": JobSourceCatalog.ApprovalState.APPROVED, "enabled": True}

#: Most listings the company view shows.
MAX_COMPANY_JOBS = 25


def _private(response):
    response["Cache-Control"] = "private, no-store"
    return response


def _not_found():
    """One body for an unknown and an inactive organization."""
    return _private(JsonResponse({"detail": "Not found."}, status=404))


def _active_organization(pk):
    """The active organization *pk*, or ``None`` (unknown and inactive alike)."""
    return Organization.objects.filter(pk=pk, status=1).first()


@login_required
@require_GET
def organization_fit_view(request, pk):
    """How this company fits the requester's own saved priorities."""
    organization = _active_organization(pk)
    if organization is None:
        return _not_found()
    return _private(JsonResponse(organization_fit(request.user, organization)))


@login_required
@require_GET
def organization_jobs_view(request, pk):
    """Active job listings of this company, newest first, capped at 25."""
    organization = _active_organization(pk)
    if organization is None:
        return _not_found()
    if not JobSourceCatalog.objects.filter(**LIVE_SOURCE).exists():
        return _private(
            JsonResponse(
                {"organization_id": organization.pk, "inventory": "unavailable", "count": 0, "results": []}
            )
        )
    listings = JobListing.objects.filter(
        organization=organization, **{f"source__{key}": value for key, value in LIVE_SOURCE.items()}
    ).select_related("organization")[:MAX_COMPANY_JOBS]
    results = []
    for listing in listings:
        payload = _listing_payload(listing)
        payload.pop("description_excerpt")
        payload.pop("organization")
        results.append(payload)
    return _private(
        JsonResponse(
            {
                "organization_id": organization.pk,
                "inventory": "available",
                "count": len(results),
                "results": results,
            }
        )
    )


__all__ = ["organization_fit_view", "organization_jobs_view"]
