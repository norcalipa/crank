# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Authorization and redaction tests for the release diagnostics view."""

import json
import os
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse


class ReleaseDiagnosticsViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.user = User.objects.create_user(username="user", password="pw")

        # ``base.html`` renders ``{% manifest 'main.js' %}``, which reads the
        # webpack manifest from STATICFILES_DIRS. Point it at a hermetic
        # fixture so the view renders without a webpack build in the test env.
        self.manifest_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.manifest_dir, ignore_errors=True)
        with open(
            os.path.join(self.manifest_dir, "manifest.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump({"main.js": "main.testhash1234.js"}, handle)
        self._manifest_settings = override_settings(
            STATICFILES_DIRS=[self.manifest_dir]
        )
        self._manifest_settings.enable()

    def tearDown(self):
        self._manifest_settings.disable()
        super().tearDown()

    def test_anonymous_is_redirected_to_admin_login(self):
        response = self.client.get(reverse("release-diagnostics"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_non_staff_is_redirected(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("release-diagnostics"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_staff_sees_page(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("release-diagnostics"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Release Diagnostics")
        self.assertContains(response, "Backend git SHA / image identifier")
        self.assertContains(response, "Frontend webpack build identifier")
        self.assertContains(response, "LLM configured")
        self.assertContains(response, "Active job listings")

    @override_settings(LLM_API_KEY="sk-super-secret", YELP_API_KEY="yelp-secret")
    def test_staff_page_never_leaks_secrets(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("release-diagnostics"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "sk-super-secret")
        self.assertNotContains(response, "yelp-secret")

    @patch(
        "crank.views.release_diagnostics.diagnostics",
        return_value={
            "git_sha": "deadbeef",
            "frontend_build_id": "1a2b3c4d5e6f",
            "build": {"status": "mismatch", "mismatched": ["frontend"]},
            "migrations": {
                "status": "error",
                "applied_count": None,
                "pending_count": None,
            },
            "config": {},
            "counts": {},
        },
    )
    def test_warning_banner_and_default_filter_render(self, _mocked):
        # A build mismatch surfaces a visible warning banner, and a None
        # migration count renders as an em dash, never the literal "None".
        self.client.force_login(self.staff)
        response = self.client.get(reverse("release-diagnostics"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Release integrity warning")
        self.assertContains(response, "mismatched: frontend")
        # ``{{ value|default:"—" }}`` renders the em dash, not the string None.
        self.assertContains(response, "\u2014")
        self.assertNotContains(response, "None")

    @patch(
        "crank.views.release_diagnostics.diagnostics",
        return_value={
            "git_sha": "unknown",
            "frontend_build_id": "unknown",
            "build": {"status": "unverifiable", "mismatched": []},
            "migrations": {"status": "clean", "applied_count": 3, "pending_count": 0},
            "config": {},
            "counts": {},
        },
    )
    def test_unverifiable_build_shows_pin_guidance(self, _mocked):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("release-diagnostics"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Release integrity warning")
        self.assertContains(response, "could not be verified")

    def _rows(self, response):
        import re

        html = response.content.decode()
        rows = dict(re.findall(r'<tr><th scope="row">([^<]+)</th><td>([^<]*)</td></tr>', html))
        return rows["Client address source"], rows["Client address for this request"]

    def test_client_address_rows_default_to_off(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("release-diagnostics"),
            HTTP_CF_CONNECTING_IP="198.51.100.7",
            HTTP_X_FORWARDED_FOR="198.51.100.8",
            REMOTE_ADDR="192.0.2.10",
        )
        # Off is worded differently from a failure to identify while on.
        self.assertEqual(self._rows(response), ("none", "off (no address is read)"))
        self.assertNotContains(response, "not identified")
        for address in ("198.51.100.7", "198.51.100.8", "192.0.2.10"):
            self.assertNotContains(response, address)

    @override_settings(
        ENV="prod",
        CLIENT_IP_SOURCE="cloudflare",
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    )
    def test_client_address_rows_when_enabled(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        self.client.force_login(self.staff)
        url = reverse("release-diagnostics")
        identified = self.client.get(
            url, HTTP_CF_CONNECTING_IP="198.51.100.7", HTTP_X_FORWARDED_FOR="198.51.100.8"
        )
        self.assertEqual(self._rows(identified), ("cloudflare", "198.51.100.7"))
        self.assertNotContains(identified, "198.51.100.8")
        network = self.client.get(url, HTTP_CF_CONNECTING_IP="2001:db8:1:2::9")
        self.assertEqual(self._rows(network), ("cloudflare", "2001:db8:1:2::/64"))
        unidentified = self.client.get(url, HTTP_X_FORWARDED_FOR="198.51.100.8")
        self.assertEqual(self._rows(unidentified), ("cloudflare", "on, not identified"))
        self.assertNotContains(unidentified, "198.51.100.8")
        for value in ("198.51.100.7, 198.51.100.8", "127.0.0.1", "10.1.2.3", "::1"):
            with self.subTest(value=value):
                refused = self.client.get(url, HTTP_CF_CONNECTING_IP=value)
                self.assertEqual(self._rows(refused), ("cloudflare", "on, not identified"))
                self.assertNotContains(refused, "198.51.100.8")
                self.assertNotContains(refused, "127.0.0.1")
                self.assertNotContains(refused, "10.1.2.3")

    @override_settings(ENV="staging", CLIENT_IP_SOURCE="cloudflare")
    def test_client_address_rows_outside_production_read_off(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("release-diagnostics"), HTTP_CF_CONNECTING_IP="198.51.100.7"
        )
        self.assertEqual(self._rows(response), ("none", "off (no address is read)"))
        self.assertNotContains(response, "198.51.100.7")

    @override_settings(ENV="prod", CLIENT_IP_SOURCE="cloudflare")
    def test_client_address_is_never_shown_to_non_staff(self):
        for login in (None, self.user):
            if login:
                self.client.force_login(login)
            response = self.client.get(
                reverse("release-diagnostics"), HTTP_CF_CONNECTING_IP="198.51.100.7"
            )
            self.assertEqual(response.status_code, 302)
            self.assertNotIn(b"198.51.100.7", response.content)
