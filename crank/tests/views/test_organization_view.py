# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from crank.models.organization import Organization


class OrganizationViewTest(TestCase):
    def setUp(self):
        cache.clear()
        self.active_org = Organization.objects.create(name="Active Org", status=1)
        self.inactive_org = Organization.objects.create(name="Inactive Org", status=0)
        self.social_app = SocialApp.objects.create(
            provider='google',
            name='Google',
            client_id='test',
            secret='test',
        )
        self.social_app.sites.add(Site.objects.get_current())

    def test_only_active_organizations_visible(self):
        response = self.client.get(reverse('organization', args=[self.active_org.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.active_org.name)

        response = self.client.get(reverse('organization', args=[self.inactive_org.id]))
        self.assertEqual(response.status_code, 404)
