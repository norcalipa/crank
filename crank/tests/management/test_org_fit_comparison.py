# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the read-only org_fit_comparison command (issue #584b)."""
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from crank.agents.jobs.matching import project_criteria
from crank.management.commands.org_fit_comparison import old_score, synthetic_profiles
from crank.models.company_profile import CompanyFieldEvidence
from crank.models.organization import Organization
from crank.models.preference import SCHEMA_VERSION, UserPreference, default_preferences


def evidence(org, field_key, value):
    return CompanyFieldEvidence.objects.create(
        organization=org,
        field_key=field_key,
        value_text=value,
        source_url="https://compare.example.test/about",
        source_domain="compare.example.test",
        observed_at=timezone.now(),
        validation_version="v1",
        extractor_version="v1",
        state=CompanyFieldEvidence.State.ACCEPTED,
    )


def run():
    out = StringIO()
    call_command("org_fit_comparison", stdout=out)
    return out.getvalue()


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class OrgFitComparisonTests(TestCase):
    def test_empty_database_prints_every_profile_with_nothing_in_it(self):
        text = run()
        self.assertIn("organizations=0", text)
        self.assertEqual(text.count("profile="), 10)
        self.assertIn("old_top10_still_in_new_top10=0 of 0", text)
        self.assertIn("largest_rise=0 largest_fall=0", text)

    def test_prints_old_and_new_top_ten_and_the_movement(self):
        # "Sleeper" was carried by a rating under the old score; "Fit" meets the priority.
        sleeper = Organization.objects.create(name="Sleeper", rto_policy="O", funding_round="A", public=True, status=1)
        fit = Organization.objects.create(name="Fit", rto_policy="R", funding_round="A", public=True, status=1)
        evidence(fit, "rto_policy", "Remote")
        with mock.patch(
            "crank.services.job_matching._company_score",
            side_effect=lambda o: 5.0 if o.pk == sleeper.pk else None,
        ):
            text = run()
        block = text.split("profile=remote-only")[1].split("profile=")[0]
        self.assertIn("survivors=2", block)
        old_part, new_part = block.split("new top 10:")
        self.assertLess(old_part.index("Sleeper"), old_part.index("Fit"))
        self.assertLess(new_part.index("Fit"), new_part.index("Sleeper"))
        self.assertIn("Fit priorities_met=1 of 1", new_part)
        self.assertIn("Sleeper priorities_met=0 of 1", new_part)
        self.assertIn("old_top10_still_in_new_top10=2 of 2", block)
        self.assertIn("largest_rise=1 (Fit) largest_fall=1 (Sleeper)", block)
        # A reader with only per-job priorities has no figure.
        per_job = text.split("profile=per-job-priorities-only")[1].split("profile=")[0]
        self.assertIn("priorities_met=-", per_job)

    def test_the_exclusion_profile_drops_the_first_company(self):
        Organization.objects.create(name="Dropped", public=True, status=1)
        Organization.objects.create(name="Kept", public=True, status=1)
        text = run()
        block = text.split("profile=public-company-with-an-exclusion")[1]
        self.assertNotIn("Dropped", block)

    def test_reads_no_user_preferences_and_writes_nothing(self):
        user = get_user_model().objects.create_user(username="reader584")
        UserPreference.objects.create(user=user, preferences=default_preferences(), schema_version=SCHEMA_VERSION)
        Organization.objects.create(name="Alpha", public=True, status=1)
        counts = (Organization.objects.count(), CompanyFieldEvidence.objects.count(), UserPreference.objects.count())
        with CaptureQueriesContext(connection) as queries:
            run()
        statements = [q["sql"] for q in queries.captured_queries]
        self.assertTrue(statements)
        self.assertFalse([s for s in statements if "crank_userpreference" in s.lower()])
        self.assertFalse([s for s in statements if s.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}])
        self.assertEqual(
            counts, (Organization.objects.count(), CompanyFieldEvidence.objects.count(), UserPreference.objects.count())
        )

    def test_old_score_is_the_removed_scorers_arithmetic(self):
        profiles = dict(synthetic_profiles("Nobody"))
        stage = project_criteria(profiles["series-b-to-d"], SCHEMA_VERSION)
        remote = project_criteria(profiles["remote-only"], SCHEMA_VERSION)
        vesting = project_criteria(profiles["accelerated-vesting"], SCHEMA_VERSION)
        organization = SimpleNamespace(
            pk=1, funding_round="B", rto_policy="R", accelerated_vesting=True, avg_scores=lambda: [{"avg_score": 5.0}]
        )
        self.assertEqual(old_score(organization, stage), 12.5 + 12.5)
        self.assertEqual(old_score(organization, remote), 12.5 + 12.5)
        self.assertEqual(old_score(organization, vesting), 12.5 + 12.5)
        bare = SimpleNamespace(pk=2, funding_round=None, rto_policy=None, accelerated_vesting=None, avg_scores=list)
        self.assertEqual(old_score(bare, vesting), 0.0)
