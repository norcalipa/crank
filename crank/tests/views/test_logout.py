# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# crank/tests/views/test_logout.py
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User
from allauth.socialaccount.models import SocialApp

class LogoutViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpassword')
        self.client.login(username='testuser', password='testpassword')

        # Create a SocialApp instance
        self.social_app = SocialApp.objects.create(
            provider='google',
            name='Google',
            client_id='fake-client-id',
            secret='fake-secret',
        )
        self.social_app.sites.add(1)  # Assuming the site ID is 1

    def test_logout_redirects_to_index(self):
        response = self.client.post(reverse('account_logout'))
        self.assertRedirects(response, reverse('index'))

    def test_logout_user(self):
        self.client.post(reverse('account_logout'))
        _ = self.client.get(reverse('index'))
        self.assertNotIn('_auth_user_id', self.client.session)