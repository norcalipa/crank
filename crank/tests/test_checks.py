# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the deployment-safety system checks (issue #397)."""
from django.test import SimpleTestCase, override_settings

from crank.checks import check_job_search_provider, is_non_dev_environment


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
