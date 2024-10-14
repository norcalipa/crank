# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.http import JsonResponse
from django.views import View
from crank.models.organization import Organization

class FundingRoundChoicesView(View):
    def get(self, request, *args, **kwargs):
        choices = Organization.get_funding_round_choices()
        return JsonResponse(choices)