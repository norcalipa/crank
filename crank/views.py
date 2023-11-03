from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import generic
from django.utils import timezone

from .models import Organization, Score


class IndexView(generic.ListView):
    template_name = "crank/index.html"
    context_object_name = "top_organization_list"

    def get_queryset(self):
        """Return the top 20 active organizations."""
        return Organization.objects.filter(status=1).order_by("-name")[:20]


class OrganizationView(generic.DetailView):
    model = Organization
    template_name = "crank/organization.html"

    def get_queryset(self):
        """make sure we can't see inactive organizations."""
        return Organization.objects.filter(status=1)

    def get_scores(self):
        return Score.objects.filter(target=self.id)
