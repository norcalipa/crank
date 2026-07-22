# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from unittest.mock import MagicMock

from django.test import RequestFactory, TestCase

from crank.adapters import SocialAccountAdapter


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
