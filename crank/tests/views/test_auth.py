# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.test import Client, TestCase, override_settings
from django.urls import reverse


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM, SECRET_KEY="test-secret")
class AuthenticationPresentationTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_expired_chat_session_returns_home_with_one_time_message(self):
        response = self.client.get(reverse("job_search"))

        self.assertRedirects(response, reverse("index"), fetch_redirect_response=False)
        self.assertNotIn("next=", response.url)

        home = self.client.get(reverse("index"))
        self.assertContains(home, "Your session has expired or you have been logged out.")
        self.assertContains(home, "Sign In")
        self.assertNotContains(self.client.get(reverse("index")), "Your session has expired")

    def test_authenticated_chat_request_is_allowed(self):
        user = self._create_user()
        self.client.force_login(user)

        response = self.client.get(reverse("job_search"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CRank")

    def test_login_page_uses_crank_chrome(self):
        response = self.client.get(reverse("account_login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CRank")
        self.assertContains(response, "Sign In")
        self.assertContains(response, "href=\"/\"")
        self.assertNotContains(response, "<strong>Menu:</strong>")

    def test_social_login_failure_uses_branded_recovery_page(self):
        response = self.client.get(reverse("socialaccount_login_error"))

        self.assertIn(response.status_code, (200, 401))
        self.assertContains(response, "CRank", status_code=response.status_code)
        self.assertContains(response, "We couldn't sign you in", status_code=response.status_code)
        self.assertContains(response, "Try signing in again", status_code=response.status_code)
        self.assertContains(response, "Return home", status_code=response.status_code)
        self.assertNotContains(response, "Third-Party Login Failure", status_code=response.status_code)

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_untrusted_login_redirect_falls_back_to_home(self):
        user = self._create_user()
        login_url = f"{reverse('account_login')}?next=https://evil.example/steal"

        response = self.client.post(
            login_url,
            {"login": user.username, "password": "password"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("index"))

    def _create_user(self):
        from django.contrib.auth.models import User

        return User.objects.create_user("test-user", password="password")
