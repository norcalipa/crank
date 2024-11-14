# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.core.cache import cache
from django.http import JsonResponse
from django.views import View
from crank.models.organization import Organization

class FundingRoundChoicesView(View):
    def get(self, request, *args, **kwargs):
        cache_key = 'funding_round_choices'
        def fetch_results():
            choices = Organization.get_funding_round_choices()
            return JsonResponse(choices)

        return cache.get_or_set(cache_key, fetch_results())
