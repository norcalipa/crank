# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the seed_e2e management command (issue #491)."""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from crank.management.commands.seed_e2e import (
    ACTIVE_LISTING_URL,
    DEFAULT_E2E_PASSWORD,
    E2E_USERNAME,
    FIXTURE_SOURCE_NAME,
    TARGET_ORGS,
)
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.organization import Organization
from crank.models.preference import UserPreference


class SeedE2ECommandTests(TestCase):
    """Dev guard, idempotency, dataset shape, and bounded summary output."""

    def test_refuses_non_dev_environment_prod(self):
        with override_settings(ENV="prod"):
            with self.assertRaises(CommandError):
                call_command("seed_e2e", stdout=StringIO())
        self.assertFalse(Organization.objects.filter(name__startswith="E2E ").exists())

    def test_refuses_non_dev_environment_staging(self):
        with override_settings(ENV="staging"):
            with self.assertRaises(CommandError):
                call_command("seed_e2e", stdout=StringIO())
        self.assertFalse(Organization.objects.filter(name__startswith="E2E ").exists())

    def test_creates_bounded_synthetic_dataset(self):
        out = StringIO()
        call_command("seed_e2e", stdout=out)

        user = get_user_model().objects.get(username=E2E_USERNAME)
        self.assertTrue(user.check_password(DEFAULT_E2E_PASSWORD))
        self.assertFalse(user.is_staff)
        # Non-default preferences so the ranked-match surface computes results.
        preference = UserPreference.objects.get(user=user)
        self.assertEqual(
            preference.preferences["work_location"]["modes"], ["remote"]
        )

        for name, _score in TARGET_ORGS:
            self.assertTrue(Organization.objects.filter(name=name).exists())
        self.assertLessEqual(Organization.objects.filter(name__startswith="E2E ").count(), 10)

        source = JobSourceCatalog.objects.get(name=FIXTURE_SOURCE_NAME)
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.APPROVED)
        self.assertTrue(source.enabled)
        listing = JobListing.all_objects.get(canonical_url=ACTIVE_LISTING_URL)
        self.assertEqual(listing.status, JobListing.Status.ACTIVE)
        self.assertTrue(listing.is_remote)
        self.assertIsNotNone(listing.organization)

    def test_is_idempotent(self):
        call_command("seed_e2e", stdout=StringIO())
        before = {
            "orgs": Organization.objects.filter(name__startswith="E2E ").count(),
            "sources": JobSourceCatalog.objects.count(),
            "listings": JobListing.all_objects.count(),
            "users": get_user_model().objects.filter(username=E2E_USERNAME).count(),
        }
        call_command("seed_e2e", stdout=StringIO())
        after = {
            "orgs": Organization.objects.filter(name__startswith="E2E ").count(),
            "sources": JobSourceCatalog.objects.count(),
            "listings": JobListing.all_objects.count(),
            "users": get_user_model().objects.filter(username=E2E_USERNAME).count(),
        }
        self.assertEqual(before, after)

    def test_password_override_is_applied_on_rerun(self):
        call_command("seed_e2e", stdout=StringIO())
        call_command("seed_e2e", "--password", "rotated-throwaway", stdout=StringIO())
        user = get_user_model().objects.get(username=E2E_USERNAME)
        self.assertTrue(user.check_password("rotated-throwaway"))
        self.assertFalse(user.check_password(DEFAULT_E2E_PASSWORD))

    def test_summary_is_bounded_and_labelled(self):
        out = StringIO()
        call_command("seed_e2e", stdout=out)
        text = out.getvalue()
        self.assertIn("seed_e2e ready", text)
        self.assertIn("users=1", text)
        # Bounded summary: a single short line, never secrets or tracebacks.
        self.assertNotIn(DEFAULT_E2E_PASSWORD, text)
        summary_lines = [line for line in text.splitlines() if "seed_e2e ready" in line]
        self.assertEqual(len(summary_lines), 1)
        self.assertLess(len(summary_lines[0]), 400)
