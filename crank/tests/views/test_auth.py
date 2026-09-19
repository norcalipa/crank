# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from urllib.parse import quote

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from crank.auth import (
    AUTH_SEEN_COOKIE,
    FIRST_VISIT_INTRO,
    SESSION_EXPIRED_MESSAGE,
    safe_next_url,
    sign_in_url,
    visitor_state,
)


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM, SECRET_KEY="test-secret")
class AuthenticationPresentationTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_anonymous_chat_request_renders_signed_out_introduction(self):
        response = self.client.get(reverse("job_search"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, FIRST_VISIT_INTRO)
        self.assertContains(response, "Sign in to save your search")
        self.assertContains(response, f"next={quote('/chat/', safe='')}")

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
        return User.objects.create_user("test-user", password="password")


@override_settings(ALLOWED_HOSTS=["testserver"])
class SafeNextUrlTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, secure=False):
        request = self.factory.get("/", secure=secure)
        return request

    def test_rejects_absolute_external_url(self):
        self.assertIsNone(safe_next_url(self._request(), "https://evil.example/x"))

    def test_rejects_protocol_relative_url(self):
        self.assertIsNone(safe_next_url(self._request(), "//evil.example/x"))

    def test_rejects_javascript_scheme(self):
        self.assertIsNone(safe_next_url(self._request(), "javascript:alert(1)"))

    def test_rejects_backslash_prefixed_url(self):
        self.assertIsNone(safe_next_url(self._request(), "\\\\evil.example"))

    def test_rejects_encoded_external_url(self):
        self.assertIsNone(safe_next_url(self._request(), "/%2f%2fevil.example/"))

    def test_rejects_different_host_same_scheme(self):
        self.assertIsNone(safe_next_url(self._request(), "http://other-host/x"))

    def test_rejects_same_host_when_https_is_required(self):
        self.assertIsNone(
            safe_next_url(self._request(secure=True), "http://testserver/private/")
        )

    def test_rejects_root_relative_url_with_invalid_control_character(self):
        self.assertIsNone(safe_next_url(self._request(), "/private/%0d%0a"))

    def test_rejects_accounts_logout_path(self):
        self.assertIsNone(safe_next_url(self._request(), "/accounts/logout/"))

    def test_rejects_empty_string(self):
        self.assertIsNone(safe_next_url(self._request(), ""))

    def test_rejects_none(self):
        self.assertIsNone(safe_next_url(self._request(), None))

    def test_accepts_root_path(self):
        self.assertEqual(safe_next_url(self._request(), "/"), "/")

    def test_accepts_chat_path(self):
        self.assertEqual(safe_next_url(self._request(), "/chat/"), "/chat/")

    def test_accepts_chat_path_with_company_query(self):
        self.assertEqual(
            safe_next_url(self._request(), "/chat/?company=7"), "/chat/?company=7"
        )

    def test_accepts_rankings_query_string(self):
        self.assertEqual(
            safe_next_url(self._request(), "/?page=2&search=acme"),
            "/?page=2&search=acme",
        )


@override_settings(ALLOWED_HOSTS=["testserver"])
class SignInUrlTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_builds_login_url_for_valid_candidate(self):
        request = self.factory.get("/")
        self.assertEqual(sign_in_url(request, "/chat/"), "/accounts/login/?next=%2Fchat%2F")

    def test_falls_back_to_home_for_rejected_candidate(self):
        request = self.factory.get("/")
        self.assertEqual(
            sign_in_url(request, "https://evil.example/x"), "/accounts/login/?next=%2F"
        )

    def test_query_string_has_exactly_one_key(self):
        request = self.factory.get("/")
        url = sign_in_url(request, "/chat/")
        query = url.split("?", 1)[1]
        self.assertEqual(len(query.split("&")), 1)


@override_settings(ALLOWED_HOSTS=["testserver"])
class VisitorStateTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_authenticated_request(self):
        user = User.objects.create_user("visitor-state-user", password="password")
        request = self.factory.get("/")
        request.user = user
        self.assertEqual(visitor_state(request), "authenticated")

    def test_anonymous_with_seen_cookie_is_session_expired(self):
        from django.contrib.auth.models import AnonymousUser

        request = self.factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES[AUTH_SEEN_COOKIE] = "1"
        self.assertEqual(visitor_state(request), "session_expired")

    def test_anonymous_without_cookie_is_first_visit(self):
        from django.contrib.auth.models import AnonymousUser

        request = self.factory.get("/")
        request.user = AnonymousUser()
        self.assertEqual(visitor_state(request), "anonymous_first_visit")


@override_settings(CACHES=LOCMEM, SECRET_KEY="test-secret")
class MarkAuthenticatedVisitTests(TestCase):
    def test_real_login_sets_the_seen_cookie(self):
        user = User.objects.create_user("cookie-user", password="password")
        client = Client()

        response = client.post(
            reverse("account_login"),
            {"login": user.username, "password": "password"},
        )

        self.assertEqual(response.status_code, 302)
        cookie = response.cookies.get(AUTH_SEEN_COOKIE)
        self.assertIsNotNone(cookie)
        self.assertEqual(cookie.value, "1")
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(bool(cookie["secure"]), False)
