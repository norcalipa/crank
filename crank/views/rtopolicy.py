# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.http import JsonResponse
from django.views import View
from crank.models.organization import Organization

class RTOPolicyChoicesView(View):
    def get(self, request, *args, **kwargs):
        choices = Organization.get_rto_policy_choices()
        return JsonResponse(choices)