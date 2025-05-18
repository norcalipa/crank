# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.http import JsonResponse
from django.core.cache import cache
from django.conf import settings
from django.shortcuts import get_object_or_404
from crank.models.organization import Organization


def funding_round_choices(request):
    """Returns all funding round choices as a JSON dict."""
    cache_key = 'funding_round_choices'
    choices = cache.get(cache_key)
    if not choices:
        choices = {key: value for key, value in Organization.FundingRound.choices}
        cache.set(cache_key, choices, timeout=settings.CACHE_MIDDLEWARE_SECONDS)
    return JsonResponse(choices)


def rto_policy_choices(request):
    """Returns all RTO policy choices as a JSON dict."""
    cache_key = 'rto_policy_choices'
    choices = cache.get(cache_key)
    if not choices:
        choices = {key: value for key, value in Organization.RTOPolicy.choices}
        cache.set(cache_key, choices, timeout=settings.CACHE_MIDDLEWARE_SECONDS)
    return JsonResponse(choices)


def organization_detail(request, pk):
    """Returns organization details as JSON."""
    cache_key = f'organization_api_{pk}'
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


def organization_scores(request, pk):
    """Returns organization scores as JSON."""
    cache_key = f'organization_scores_api_{pk}'
    scores_data = cache.get(cache_key)
    
    if not scores_data:
        organization = get_object_or_404(Organization, pk=pk, status=1)
        scores = organization.avg_scores()
        scores_data = list(scores)
        cache.set(cache_key, scores_data, timeout=settings.CACHE_MIDDLEWARE_SECONDS)
    
    return JsonResponse(scores_data, safe=False) 