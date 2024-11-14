# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.core.cache import cache
from django.http import JsonResponse
from django.views import View
from crank.models.organization import Organization

class RTOPolicyChoicesView(View):
    def get(self, request, *args, **kwargs):
        cache_key = 'policy_choices'

        def fetch_results():
            choices = Organization.get_rto_policy_choices()
            return JsonResponse(choices)

        return cache.get_or_set(cache_key, fetch_results())