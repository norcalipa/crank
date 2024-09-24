from django.http import JsonResponse
from django.views import View
from crank.models.organization import Organization

class RTOPolicyChoicesView(View):
    def get(self, request, *args, **kwargs):
        choices = Organization.get_rto_policy_choices()
        return JsonResponse(choices)