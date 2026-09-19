# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from crank.auth import AUTH_SEEN_COOKIE, SESSION_EXPIRED_MESSAGE
from crank.models import JobSearchConversation, JobSearchMessage, Organization


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM, SECRET_KEY="test-secret")
class JobSearchPageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("job_search")

    def test_anonymous_get_creates_no_conversation_rows(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(JobSearchConversation.objects.count(), 0)
        self.assertEqual(JobSearchMessage.objects.count(), 0)

    def test_response_is_never_cached(self):
        response = self.client.get(self.url)

        cache_control = response.headers["Cache-Control"]
        self.assertIn("no-store", cache_control)
        self.assertIn("private", cache_control)

    def test_signed_out_body_contains_no_username(self):
        user = User.objects.create_user("secret-username", password="password")

        response = self.client.get(self.url)

        self.assertNotContains(response, user.username)

    def test_anonymous_with_seen_cookie_shows_expiry_explanation(self):
        owner = User.objects.create_user("other-account", password="password")
        conversation = JobSearchConversation.objects.create(owner=owner)
        JobSearchMessage.objects.create(
            conversation=conversation,
            role=JobSearchMessage.Role.USER,
            content="a secret message only the owner should see",
        )
        self.client.cookies[AUTH_SEEN_COOKIE] = "1"

        response = self.client.get(self.url)

        self.assertContains(response, SESSION_EXPIRED_MESSAGE)
        self.assertNotContains(response, "a secret message only the owner should see")

    def test_anonymous_without_cookie_shows_first_visit_introduction(self):
        response = self.client.get(self.url)

        self.assertNotContains(response, SESSION_EXPIRED_MESSAGE)

    def test_authenticated_get_renders_authenticated_dataset(self):
        user = User.objects.create_user("chat-user", password="password")
        self.client.force_login(user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-authenticated="true"')
        self.assertNotContains(response, "Sign in to save your search")

    def test_sign_in_url_preserves_only_validated_company_context(self):
        org = Organization.objects.create(
            name="Context Organization",
            status=1,
            type="C",
            url="https://example.com",
            gives_ratings=True,
            public=True,
            accelerated_vesting=True,
            funding_round="S",
            rto_policy="R",
        )

        response = self.client.get(
            self.url,
            {"company": org.id, "message": "private draft", "salary": "secret"},
        )

        sign_in_url = response.context["sign_in_url"]
        self.assertIn("company%3D{}".format(org.id), sign_in_url)
        self.assertNotIn("private", sign_in_url)
        self.assertNotIn("salary", sign_in_url)
        self.assertNotIn("message", sign_in_url)

    def test_company_query_param_resolves_existing_organization(self):
        org = Organization.objects.create(
            name="Test Organization",
            status=1,
            type="C",
            url="https://example.com",
            gives_ratings=True,
            public=True,
            accelerated_vesting=True,
            funding_round="S",
            rto_policy="R",
        )

        response = self.client.get(self.url, {"company": org.id})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_company_id"], org.id)
        self.assertContains(response, f'data-selected-company-id="{org.id}"')

    def test_non_integer_company_query_param_is_ignored(self):
        response = self.client.get(self.url, {"company": "abc"})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["selected_company_id"])

    def test_unknown_company_id_is_ignored(self):
        response = self.client.get(self.url, {"company": 999999})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["selected_company_id"])
