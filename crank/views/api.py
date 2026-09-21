# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.http import JsonResponse
from django.core.cache import cache
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import ensure_csrf_cookie

from crank.models.organization import Organization
from crank.models.company_profile import CompanyProfileObservation
from crank.services.company_evidence import field_evidence_payload
from crank.services.scores import (
    organization_api_cache_key,
    organization_provenance_api_cache_key,
    organization_scores_api_cache_key,
)


def organization_detail(request, pk):
    """Returns organization details as JSON."""
    cache_key = organization_api_cache_key(pk)
    org_data = cache.get(cache_key)

    if not org_data:
        organization = get_object_or_404(Organization, pk=pk, status=1)
        org_data = {
            'id': organization.id,
            'name': organization.name,
            'type': organization.type,
            'url': organization.url,
            'gives_ratings': organization.gives_ratings,
            'public': organization.public,
            'accelerated_vesting': organization.accelerated_vesting,
            'funding_round': organization.funding_round,
            'rto_policy': organization.rto_policy,
        }
        cache.set(cache_key, org_data, timeout=settings.CACHE_MIDDLEWARE_SECONDS)

    return JsonResponse(org_data)


def organization_provenance(request, pk):
    """Returns bounded provenance and freshness metadata for an organization.

    Surfaces:
    - Organization created/modified timestamps (curated data freshness).
    - Latest CompanyProfileObservation source label, observed_at, extraction_version,
      and status — without exposing raw crawl HTML, credentials, or unsafe URLs.
      ``is_verified`` marks whether that observation is accepted evidence; a
      pending/rejected/conflicted observation stays inspectable but is never
      presented as verification (issue #460).
    - ``fields``: one entry per field with accepted evidence, including scope,
      the four freshness timestamps, and a per-response ``stale`` flag.
    - ``unverified_fields``: registered field keys with no accepted evidence —
      missing evidence is explicit.
    - Profile completeness percentage for trust context.
    """
    cache_key = organization_provenance_api_cache_key(pk)
    prov_data = cache.get(cache_key)
    if prov_data and 'fields' not in prov_data:
        # Entry written before this deploy: the key is unchanged (the shared
        # constructor also drives outbox invalidation, so versioning it here
        # would desynchronize that), and the payload predates the evidence
        # arrays. The frontend guards on ``provenance.fields``, so serving it
        # would render the modal with no evidence section at all —
        # indistinguishable from "no evidence exists". Treat it as a miss and
        # rebuild once, instead of degrading for the rest of the TTL.
        prov_data = None

    if not prov_data:
        organization = get_object_or_404(Organization, pk=pk, status=1)
        prov_data = {
            'organization_id': organization.id,
            'organization_modified': organization.modified.isoformat() if organization.modified else None,
            'organization_created': organization.created.isoformat() if organization.created else None,
            'latest_observation': None,
        }

        latest_obs = (
            CompanyProfileObservation.objects
            .filter(organization_id=organization.id)
            .order_by('-observed_at', '-id')
            .first()
        )
        if latest_obs:
            prov_data['latest_observation'] = {
                'source_url': latest_obs.source_url,
                'observed_domain': latest_obs.observed_domain,
                'observed_at': latest_obs.observed_at.isoformat(),
                'extraction_version': latest_obs.extraction_version,
                'status': latest_obs.status,
                'is_verified': latest_obs.status in (
                    CompanyProfileObservation.Status.ACCEPTED,
                    CompanyProfileObservation.Status.AUTO_APPLIED,
                ),
            }

        prov_data.update(field_evidence_payload(organization))

        cache.set(cache_key, prov_data, timeout=settings.CACHE_MIDDLEWARE_SECONDS)

    return JsonResponse(prov_data)


def organization_scores(request, pk):
    """Returns organization scores as JSON."""
    cache_key = organization_scores_api_cache_key(pk)
    scores_data = cache.get(cache_key)

    if not scores_data:
        organization = get_object_or_404(Organization, pk=pk, status=1)
        scores = organization.avg_scores()
        scores_data = list(scores)
        cache.set(cache_key, scores_data, timeout=settings.CACHE_MIDDLEWARE_SECONDS)

    return JsonResponse(scores_data, safe=False)


@ensure_csrf_cookie
def account_whoami(request):
    """Return the caller's own bounded account identity for nav hydration.

    Per-user identity must never live in the shared page caches (issue #470):
    the full-page-cached shell is rendered auth-neutral and the client fills
    the account label and auth-dependent visibility per request from this
    endpoint. It exposes only the caller's own username and nothing else.

    It is marked non-cacheable (``Cache-Control: private, no-store``) so
    browsers and intermediaries never replay a stale identity across account
    switches, and ``@ensure_csrf_cookie`` guarantees a CSRF cookie exists so
    the cached shell can submit a token-checked logout without embedding a
    session-bound token.
    """
    if not request.user.is_authenticated:
        response = JsonResponse({"authenticated": False})
    else:
        response = JsonResponse(
            {"authenticated": True, "username": request.user.username}
        )
    response["Cache-Control"] = "private, no-store"
    return response
