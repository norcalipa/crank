# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the deployment-safety system checks (issue #397)."""
import os
import types
from unittest.mock import patch

from django.conf import settings as django_settings
from django.test import SimpleTestCase, override_settings

from crank.checks import (
    check_capability_config,
    check_client_ip_source,
    check_job_search_provider,
    is_non_dev_environment,
)


class IsNonDevEnvironmentTests(SimpleTestCase):
    def test_default_dev_environment_is_dev(self):
        # crank.settings defaults to dev (no ENV set).
        self.assertFalse(is_non_dev_environment())

    @override_settings(ENV="")
    def test_blank_env_is_dev(self):
        self.assertFalse(is_non_dev_environment())

    @override_settings(ENV="dev")
    def test_explicit_dev_is_dev(self):
        self.assertFalse(is_non_dev_environment())

    @override_settings(ENV="prod")
    def test_env_prod_is_non_dev(self):
        self.assertTrue(is_non_dev_environment())

    @override_settings(ENV="staging")
    def test_env_staging_is_non_dev(self):
        self.assertTrue(is_non_dev_environment())

    @override_settings(ENV="PROD")
    def test_env_is_case_insensitive(self):
        self.assertTrue(is_non_dev_environment())

    def test_os_env_var_is_fallback_when_settings_lacks_env(self):
        # Production shape (issue #423): the deployment signal is the OS
        # ``ENV`` var that ``crank.settings`` branches on. Even if a settings
        # object does not carry ``ENV``, the real deployment signal must fire.
        with patch.dict(os.environ, {"ENV": "prod"}):
            with override_settings(ENV=""):
                self.assertTrue(is_non_dev_environment())

    def test_os_env_var_staging_is_non_dev(self):
        with patch.dict(os.environ, {"ENV": "staging"}):
            with override_settings(ENV=""):
                self.assertTrue(is_non_dev_environment())

    def test_os_env_var_is_case_insensitive_fallback(self):
        with patch.dict(os.environ, {"ENV": "PROD"}):
            with override_settings(ENV=""):
                self.assertTrue(is_non_dev_environment())

    def test_settings_modules_declare_env(self):
        # Each settings module now declares ENV so ``settings.ENV`` is
        # populated in a real deploy (the authoritative signal).
        import importlib

        dev = importlib.import_module("crank.settings.dev")
        self.assertEqual(dev.ENV, "dev")

    def test_prod_and_staging_settings_modules_declare_env(self):
        # Import the non-dev settings modules (with the optional MySQL pool
        # backend stubbed out of the environment) so their deployment-selected
        # ``ENV`` values actually execute and are covered.
        import importlib
        import sys

        _pool = types.ModuleType("dj_db_conn_pool")
        _backends = types.ModuleType("dj_db_conn_pool.backends")
        _mysql = types.ModuleType("dj_db_conn_pool.backends.mysql")
        _pool.backends = _backends
        _backends.mysql = _mysql
        with patch.dict(
            sys.modules,
            {
                "dj_db_conn_pool": _pool,
                "dj_db_conn_pool.backends": _backends,
                "dj_db_conn_pool.backends.mysql": _mysql,
            },
        ):
            prod = importlib.import_module("crank.settings.prod")
            staging = importlib.import_module("crank.settings.staging")
        self.assertEqual(prod.ENV, "prod")
        self.assertEqual(staging.ENV, "staging")


class CheckJobSearchProviderTests(SimpleTestCase):
    @override_settings(JOB_SEARCH_PROVIDER="demo", ENV="prod")
    def test_demo_in_prod_warns(self):
        errors = check_job_search_provider()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, "crank.W001")
        self.assertIn("demo", errors[0].msg)

    @override_settings(JOB_SEARCH_PROVIDER="demo", ENV="staging")
    def test_demo_in_staging_warns(self):
        self.assertEqual(len(check_job_search_provider()), 1)

    @override_settings(JOB_SEARCH_PROVIDER="demo", ENV="dev")
    def test_demo_in_dev_is_fine(self):
        self.assertEqual(check_job_search_provider(), [])

    @override_settings(JOB_SEARCH_PROVIDER="orchestrator", ENV="prod")
    def test_orchestrator_in_prod_is_fine(self):
        self.assertEqual(check_job_search_provider(), [])

    @override_settings(JOB_SEARCH_PROVIDER="", ENV="prod")
    def test_unset_provider_in_prod_is_fine(self):
        self.assertEqual(check_job_search_provider(), [])

    def test_check_is_registered(self):
        # Importing `crank.checks` registers the check; confirm it is visible
        # to `manage.py check` via the global registry.
        from django.core.checks.registry import registry

        self.assertIn(check_job_search_provider, registry.registered_checks)
        # The AppConfig is wired for startup registration too.
        from django.apps import apps

        self.assertEqual(apps.get_app_config("crank").name, "crank")


class CheckCapabilityConfigTests(SimpleTestCase):
    def test_no_issues_when_all_disabled(self):
        self.assertEqual(check_capability_config(), [])

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="crank.agents.llm:FakeLLMProvider",
        LLM_MODEL="test",
        LLM_API_KEY="test",
    )
    def test_no_issues_when_enabled_and_configured(self):
        self.assertEqual(check_capability_config(), [])

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="",
        LLM_MODEL="",
        LLM_API_KEY="",
    )
    def test_warning_when_enabled_but_misconfigured(self):
        errors = check_capability_config()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, "crank.W002")
        self.assertIn("missing required configuration", errors[0].msg)

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="",
        LLM_MODEL="",
        LLM_API_KEY="sk-super-secret",
    )
    def test_warning_never_leaks_secret_values(self):
        errors = check_capability_config()
        self.assertEqual(len(errors), 1)
        import json

        blob = json.dumps(errors[0].msg)
        self.assertNotIn("sk-super-secret", blob)

    def test_capability_check_is_registered(self):
        from django.core.checks.registry import registry

        self.assertIn(check_capability_config, registry.registered_checks)


class CheckClientIpSourceTests(SimpleTestCase):
    """``crank.W003`` / ``crank.W004`` (issue #538): warn, never block boot."""

    # On newer Django, ``call_command("check")`` also runs the database checks.
    databases = frozenset({"default"})

    def setUp(self):
        patcher = patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLIENT_IP_SOURCE", None)

    def ids(self):
        return [warning.id for warning in check_client_ip_source()]

    def test_default_is_silent(self):
        self.assertEqual(check_client_ip_source(), [])

    def test_empty_is_silent_in_every_environment(self):
        for env in ("dev", "staging", "prod"):
            with self.subTest(env=env), override_settings(ENV=env, CLIENT_IP_SOURCE=""):
                self.assertEqual(check_client_ip_source(), [])

    @override_settings(ENV="prod", CLIENT_IP_SOURCE="cloudflare")
    def test_recognised_source_in_production_is_silent(self):
        self.assertEqual(check_client_ip_source(), [])

    def test_unrecognised_value_in_production_warns_w003(self):
        for value in ("x-forwarded-for", "remote_addr", "Cloudflare", " cloudflare", None, 1, True):
            with self.subTest(value=value), override_settings(ENV="prod", CLIENT_IP_SOURCE=value):
                self.assertEqual(self.ids(), ["crank.W003"])

    def test_recognised_source_outside_production_warns_w004(self):
        for env in ("dev", "staging", ""):
            with self.subTest(env=env), override_settings(ENV=env, CLIENT_IP_SOURCE="cloudflare"):
                self.assertEqual(self.ids(), ["crank.W004"])

    @override_settings(ENV="dev", CLIENT_IP_SOURCE="x-forwarded-for")
    def test_any_value_outside_production_warns_w004_only(self):
        # Outside production every value is ignored for the same reason.
        self.assertEqual(self.ids(), ["crank.W004"])

    def test_environment_variable_outside_production_warns_w004(self):
        """Only prod.py reads the variable, so the check reads the environment."""
        for env in ("dev", "staging", ""):
            for value in ("cloudflare", "x-forwarded-for", " Cloudflare "):
                with self.subTest(env=env, value=value), override_settings(ENV=env):
                    os.environ["CLIENT_IP_SOURCE"] = value
                    self.assertEqual(django_settings.CLIENT_IP_SOURCE, "")
                    self.assertEqual(self.ids(), ["crank.W004"])

    def test_blank_environment_variable_outside_production_is_silent(self):
        for value in ("", "   "):
            with self.subTest(value=value), override_settings(ENV="dev"):
                os.environ["CLIENT_IP_SOURCE"] = value
                self.assertEqual(self.ids(), [])

    def test_production_judges_the_setting_not_the_environment(self):
        os.environ["CLIENT_IP_SOURCE"] = "x-forwarded-for"
        with override_settings(ENV="prod", CLIENT_IP_SOURCE="cloudflare"):
            self.assertEqual(self.ids(), [])
        with override_settings(ENV="prod", CLIENT_IP_SOURCE=""):
            self.assertEqual(self.ids(), [])

    @override_settings(ENV="dev")
    def test_manage_check_reports_w004_from_the_environment(self):
        from io import StringIO

        from django.core.management import call_command

        os.environ["CLIENT_IP_SOURCE"] = "cloudflare"
        stderr = StringIO()
        call_command("check", stdout=StringIO(), stderr=stderr)
        self.assertIn("crank.W004", stderr.getvalue())

    @override_settings(ENV="prod", CLIENT_IP_SOURCE="https://user:hunter2@example.invalid/")
    def test_warnings_are_warnings_and_never_echo_the_value(self):
        from django.core.checks import WARNING

        from crank.client_address import effective_source

        (warning,) = check_client_ip_source()
        self.assertEqual(warning.level, WARNING)
        self.assertNotIn("hunter2", f"{warning.msg} {warning.hint}")
        self.assertIn("docs/runbook-client-address.md", warning.hint)
        self.assertIn("cloudflare", warning.hint)
        # The value the check warns about is the value the resolver ignores.
        self.assertEqual(effective_source(), "")

    def test_ignored_values_resolve_no_source(self):
        from crank.client_address import effective_source

        for env, value in (("dev", "cloudflare"), ("prod", "x-forwarded-for")):
            with self.subTest(env=env), override_settings(ENV=env, CLIENT_IP_SOURCE=value):
                self.assertTrue(check_client_ip_source())
                self.assertEqual(effective_source(), "")

    def test_client_ip_source_check_is_registered(self):
        from django.core.checks.registry import registry

        self.assertIn(check_client_ip_source, registry.registered_checks)

    @override_settings(ENV="prod", CLIENT_IP_SOURCE="nonsense")
    def test_manage_check_does_not_fail_on_a_bad_value(self):
        from io import StringIO

        from django.core.management import call_command

        stderr = StringIO()
        call_command("check", stdout=StringIO(), stderr=stderr)
        self.assertIn("crank.W003", stderr.getvalue())
