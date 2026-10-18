# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from unittest.mock import MagicMock

from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from crank.adapters import AccountAdapter, SocialAccountAdapter

User = get_user_model()


class SocialAccountAdapterTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.adapter = SocialAccountAdapter()
        self.request = self.factory.get("/accounts/google/login/callback/")

    def test_on_authentication_error_logs_provider_path_error_exception(self):
        provider = MagicMock(id="google")
        exception = ValueError("boom")

        with self.assertLogs("users", level="WARNING") as captured:
            result = self.adapter.on_authentication_error(
                self.request,
                provider,
                error="unknown",
                exception=exception,
            )

        # The default adapter is a no-op and returns None; rendering is handled
        # by allauth's render_authentication_error() via ImmediateHttpResponse.
        self.assertIsNone(result)
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertIn("google", message)
        self.assertIn("/accounts/google/login/callback/", message)
        self.assertIn("unknown", message)
        self.assertIn("boom", message)
        # exc_info must be attached so the traceback is logged, not just the str.
        self.assertIs(captured.records[0].exc_info[1], exception)

    def test_on_authentication_error_accepts_provider_string(self):
        with self.assertLogs("users", level="WARNING") as captured:
            self.adapter.on_authentication_error(
                self.request,
                "google",
                error="cancelled",
                exception=None,
            )

        self.assertEqual(len(captured.records), 1)
        self.assertIn("google", captured.records[0].getMessage())


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
ENABLED = {"ENV": "prod", "CLIENT_IP_SOURCE": "cloudflare", "CACHES": LOCMEM}


class AccountAdapterClientIpTests(TestCase):
    """Criteria 1-3: the adapter binds allauth's address to client_address()."""

    def setUp(self):
        self.factory = RequestFactory()
        self.adapter = AccountAdapter()

    def _get(self, **meta):
        return self.factory.get("/accounts/login/", **meta)

    def test_account_adapter_is_configured(self):
        self.assertEqual(settings.ACCOUNT_ADAPTER, "crank.adapters.AccountAdapter")

    @override_settings(**ENABLED)
    def test_identified_ipv4_ignores_forwarded_for_and_remote_addr(self):
        request = self._get(
            HTTP_CF_CONNECTING_IP="198.51.100.7",
            HTTP_X_FORWARDED_FOR="203.0.113.9, 192.0.2.1",
            REMOTE_ADDR="10.0.0.1",
        )

        self.assertEqual(self.adapter.get_client_ip(request), "198.51.100.7")

    @override_settings(**ENABLED)
    def test_identified_ipv6_returns_the_slash_64_bucket(self):
        request = self._get(
            HTTP_CF_CONNECTING_IP="2001:db8:1:2:aaaa:bbbb:cccc:dddd",
            HTTP_X_FORWARDED_FOR="203.0.113.9",
        )

        self.assertEqual(self.adapter.get_client_ip(request), "2001:db8:1:2::/64")

    def test_switch_off_equals_the_default_adapter(self):
        for meta in (
            {"HTTP_X_FORWARDED_FOR": "203.0.113.9, 192.0.2.1", "REMOTE_ADDR": "10.0.0.1"},
            {"REMOTE_ADDR": "10.0.0.1"},
            {"HTTP_CF_CONNECTING_IP": "198.51.100.7", "REMOTE_ADDR": "10.0.0.1"},
        ):
            with self.subTest(meta=meta):
                request = self._get(**meta)
                self.assertEqual(
                    self.adapter.get_client_ip(request),
                    DefaultAccountAdapter().get_client_ip(request),
                )

    @override_settings(ENV="dev", CLIENT_IP_SOURCE="cloudflare", CACHES=LOCMEM)
    def test_outside_production_equals_the_default_adapter(self):
        request = self._get(
            HTTP_CF_CONNECTING_IP="198.51.100.7", HTTP_X_FORWARDED_FOR="203.0.113.9"
        )

        self.assertEqual(self.adapter.get_client_ip(request), "203.0.113.9")

    @override_settings(**ENABLED)
    def test_unidentified_request_falls_back_to_the_default_adapter(self):
        for meta in (
            {},
            {"HTTP_CF_CONNECTING_IP": "not-an-address"},
            {"HTTP_CF_CONNECTING_IP": "198.51.100.7, 198.51.100.8"},
        ):
            with self.subTest(meta=meta):
                request = self._get(
                    HTTP_X_FORWARDED_FOR="203.0.113.9", REMOTE_ADDR="10.0.0.1", **meta
                )
                self.assertEqual(
                    self.adapter.get_client_ip(request),
                    DefaultAccountAdapter().get_client_ip(request),
                )


@override_settings(CACHES=LOCMEM, SECRET_KEY="test-secret")
class AllauthRateLimitEndToEndTests(TestCase):
    """Criteria 4-5: the signup and sign-in limits bind the trusted address."""

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.n = 0

    def _signup(self, **headers):
        self.n += 1
        return self.client.post(
            reverse("account_signup"),
            {
                "username": f"limit-user-{self.n}",
                "email": f"limit-{self.n}@example.com",
                "password1": "correct-horse-battery-9",
                "password2": "correct-horse-battery-9",
            },
            **headers,
        )

    @override_settings(ACCOUNT_RATE_LIMITS={"signup": "1/m/ip"}, **ENABLED)
    def test_one_trusted_address_shares_the_limit_whatever_forwarded_for_says(self):
        first = self._signup(HTTP_CF_CONNECTING_IP="198.51.100.7", HTTP_X_FORWARDED_FOR="1.1.1.1")
        second = self._signup(HTTP_CF_CONNECTING_IP="198.51.100.7", HTTP_X_FORWARDED_FOR="2.2.2.2")

        self.assertNotEqual(first.status_code, 429)
        self.assertEqual(second.status_code, 429)

    @override_settings(ACCOUNT_RATE_LIMITS={"signup": "1/m/ip"}, **ENABLED)
    def test_two_trusted_addresses_do_not_share_the_limit(self):
        first = self._signup(HTTP_CF_CONNECTING_IP="198.51.100.7", HTTP_X_FORWARDED_FOR="1.1.1.1")
        second = self._signup(HTTP_CF_CONNECTING_IP="198.51.100.8", HTTP_X_FORWARDED_FOR="1.1.1.1")

        self.assertNotEqual(first.status_code, 429)
        self.assertNotEqual(second.status_code, 429)

    @override_settings(ACCOUNT_RATE_LIMITS={"signup": "1/m/ip"}, ENV="prod", CLIENT_IP_SOURCE="")
    def test_switch_off_keeps_todays_leftmost_forwarded_for_behaviour(self):
        different = [
            self._signup(HTTP_CF_CONNECTING_IP="198.51.100.7", HTTP_X_FORWARDED_FOR="1.1.1.1"),
            self._signup(HTTP_CF_CONNECTING_IP="198.51.100.7", HTTP_X_FORWARDED_FOR="2.2.2.2"),
        ]
        same = self._signup(HTTP_X_FORWARDED_FOR="2.2.2.2")

        self.assertNotIn(429, [r.status_code for r in different])
        self.assertEqual(same.status_code, 429)

    @override_settings(ACCOUNT_RATE_LIMITS={"signup": "1/m/ip"}, **ENABLED)
    def test_signup_429_renders_the_page(self):
        self._signup(HTTP_CF_CONNECTING_IP="198.51.100.7")
        response = self._signup(HTTP_CF_CONNECTING_IP="198.51.100.7")

        self.assertEqual(response.status_code, 429)
        self.assertTemplateUsed(response, "429.html")
        self.assertContains(response, "Too many attempts", status_code=429)
        self.assertContains(response, "Please wait a minute and try again.", status_code=429)
        self.assertContains(response, 'href="/"', status_code=429)

    @override_settings(ACCOUNT_RATE_LIMITS={"login": "1/m/ip"}, **ENABLED)
    def test_signin_429_renders_the_page(self):
        User.objects.create_user("limit-login", password="password")
        payload = {"login": "limit-login", "password": "password"}
        self.client.post(reverse("account_login"), payload, HTTP_CF_CONNECTING_IP="198.51.100.7")
        self.client.logout()
        response = self.client.post(
            reverse("account_login"), payload, HTTP_CF_CONNECTING_IP="198.51.100.7"
        )

        self.assertEqual(response.status_code, 429)
        self.assertTemplateUsed(response, "429.html")
        self.assertContains(response, "Too many attempts", status_code=429)
