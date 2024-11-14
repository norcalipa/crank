# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.conf import settings
from django.core.cache import cache
from django.views import generic

# from crank.models.score import Score
from crank.models.organization import Organization


class OrganizationView(generic.DetailView):
    model = Organization
    template_name = "crank/organization.html"

    def get_queryset(self):
        """make sure we can't see inactive organizations."""
        return Organization.objects.filter(status=1)

    def get_object(self, queryset=None):
        obj_id = self.kwargs.get('pk')
        cache_key = f'organization_{obj_id}'
        return cache.get_or_set(cache_key, lambda: super(OrganizationView, self).get_object(queryset),
                                timeout=settings.CACHE_MIDDLEWARE_SECONDS)
