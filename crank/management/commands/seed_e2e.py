# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Seed the dev-only Django-backed Playwright E2E dataset (issue #491).

Creates a bounded, fully synthetic, clearly labeled dataset so the browser
suite can exercise every journey state against a real Django server:

- a password test account (``e2e_user``) with a non-default preference
  document so the ranked-match surface computes live results;
- a bounded set of organizations and Culture scores feeding the default
  rankings algorithm (including one maximal-length name for long-content
  layout checks);
- an approved+enabled ``JobSourceCatalog`` fixture source (never one of the
  curated ``seed_job_sources`` rows) with active ``JobListing`` rows.

The command is idempotent (``get_or_create`` everywhere) and refuses to run
in a non-dev environment, so synthetic E2E data can never reach staging or
production. It prints a bounded summary and never prints secrets.

The ``seeds/*.yaml`` files have no runtime loader in this repository (they
are loaded out-of-band); this command intentionally reuses the code-owned
``SEED_SOURCES`` constant instead, mirroring ``seed_staging_baseline``.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crank.checks import is_non_dev_environment
from crank.management.commands.seed_job_sources import SEED_SOURCES
from crank.models.company_profile import CompanyProfileObservation
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.organization import Organization
from crank.models.preference import UserPreference, default_preferences
from crank.models.score import Score, ScoreAlgorithm, ScoreAlgorithmWeight, ScoreType
from crank.settings.base import DEFAULT_ALGORITHM_ID

#: Throwaway local credential for the E2E account. Dev-only: the command
#: refuses to run outside dev, and operators may override it with --password.
DEFAULT_E2E_PASSWORD = "e2e-throwaway-password"

E2E_USERNAME = "e2e_user"

#: Fixture names are unique keys, which makes re-runs idempotent and keeps
#: the dataset obviously synthetic ("E2E " prefix everywhere).
RATING_SOURCE_ORG_NAME = "E2E Rating Source"
FIXTURE_SOURCE_NAME = "E2E Seed Source"

#: Bounded org/score set: (name, avg score). The last entry intentionally
#: carries a maximal-length name for long-content layout assertions.
TARGET_ORGS: list[tuple[str, float]] = [
    ("E2E Alpha Corp", 4.8),
    ("E2E Beta Labs", 4.2),
    ("E2E Gamma Works", 3.5),
    (
        "E2E " + "Longname " * 11 + "Corp",  # 100 chars: CharField max_length
        2.9,
    ),
]

ACTIVE_LISTING_URL = "https://www.usajobs.gov/Job/e2e-seed-active-1"
ACTIVE_LISTING_TITLE = "E2E Seed Software Engineer"


class Command(BaseCommand):
    help = (
        "Seed the dev-only Playwright E2E dataset (test account, bounded "
        "organizations/scores, approved+enabled source with active listings). "
        "Refuses to run in a non-dev environment."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=DEFAULT_E2E_PASSWORD,
            help="Throwaway password for the local E2E test account.",
        )

    def handle(self, *args, **options):
        if is_non_dev_environment():
            raise CommandError(
                "seed_e2e refuses to run in a non-dev environment "
                f"(ENV={getattr(settings, 'ENV', '')!r}); synthetic E2E data "
                "must never enter staging or production."
            )
        summary = self._load(password=options["password"])
        self.stdout.write(
            self.style.SUCCESS(
                "seed_e2e ready (idempotent re-run safe): "
                + ", ".join(f"{key}={value}" for key, value in sorted(summary.items()))
            )
        )

    @transaction.atomic
    def _load(self, *, password: str) -> dict[str, int]:
        now = timezone.now()
        created: dict[str, int] = {}

        # -- Rankings dependencies: default algorithm + Culture weight --------
        score_type, _ = ScoreType.objects.get_or_create(name="Culture")
        algorithm, _ = ScoreAlgorithm.objects.get_or_create(
            id=DEFAULT_ALGORITHM_ID,
            defaults={
                "name": "E2E Default Algorithm",
                # IndexView renders CONTENT_DIR/<description_content>; point at
                # a repository-owned content file so the detail panel works.
                "description_content": "culture-focused.md",
            },
        )
        # Self-heal databases that saw an earlier seed revision.
        if algorithm.description_content != "culture-focused.md":
            algorithm.description_content = "culture-focused.md"
            algorithm.save(update_fields=["description_content", "modified"])
        ScoreAlgorithmWeight.objects.get_or_create(
            algorithm=algorithm,
            type=score_type,
            defaults={"weight": 1.0},
        )

        # -- Rating source + bounded target organizations --------------------
        rating_source, _ = Organization.objects.get_or_create(
            name=RATING_SOURCE_ORG_NAME,
            defaults={
                "type": Organization.Type.COMPANY,
                "url": "https://e2e.example.test/ratings",
                "gives_ratings": True,
                "public": True,
                "funding_round": Organization.FundingRound.PUBLIC,
                "rto_policy": Organization.RTOPolicy.HYBRID,
            },
        )
        orgs: dict[str, Organization] = {}
        for name, score_value in TARGET_ORGS:
            org, _ = Organization.objects.get_or_create(
                name=name,
                defaults={
                    "type": Organization.Type.COMPANY,
                    "url": "https://e2e.example.test/",
                    "gives_ratings": False,
                    "public": True,
                    "funding_round": Organization.FundingRound.PUBLIC,
                    "rto_policy": Organization.RTOPolicy.HYBRID,
                },
            )
            orgs[name] = org
            Score.objects.get_or_create(
                type=score_type,
                source=rating_source,
                target=org,
                status=1,
                defaults={
                    "score": score_value,
                    "low_threshold": 0.0,
                    "high_threshold": 5.0,
                    "activate_date": now - timedelta(days=30),
                    "deactivate_date": None,
                },
            )
        created["organizations"] = Organization.objects.filter(
            name__startswith="E2E "
        ).count()

        # -- Approved+enabled fixture source with an active listing ----------
        # Distinct name so curated seed_job_sources policy is never rewritten.
        usajobs = next(
            entry for entry in SEED_SOURCES if entry["adapter_key"] == "usajobs"
        )
        fixture_source, _ = JobSourceCatalog.objects.get_or_create(
            name=FIXTURE_SOURCE_NAME,
            defaults={
                "adapter_key": usajobs["adapter_key"],
                "base_url": usajobs["base_url"],
                "approval_state": JobSourceCatalog.ApprovalState.APPROVED,
                "enabled": True,
                "catalog_metadata": {
                    "description": "E2E fixture source (approved+enabled, dev only).",
                },
            },
        )
        alpha = orgs[TARGET_ORGS[0][0]]

        # -- Accepted company-profile observation (evidence section) ---------
        CompanyProfileObservation.objects.get_or_create(
            fingerprint="e2e-accepted",
            defaults={
                "organization": alpha,
                "source_url": "https://e2e.example.test/alpha/profile",
                "observed_domain": "e2e.example.test",
                "observed_name": alpha.name,
                "extraction_version": "e2e-seed-1",
                "observed_at": now,
                "status": CompanyProfileObservation.Status.ACCEPTED,
                "description": "Fresh accepted E2E profile evidence.",
            },
        )
        JobListing.all_objects.get_or_create(
            source=fixture_source,
            canonical_url=ACTIVE_LISTING_URL,
            defaults={
                "external_id": "e2e-seed-active-1",
                "employer_name": alpha.name,
                "employer_domain": "e2e.example.test",
                "title": ACTIVE_LISTING_TITLE,
                "location_text": "Remote",
                "is_remote": True,
                "compensation_min": 120000,
                "compensation_max": 160000,
                "compensation_currency": "USD",
                "compensation_interval": "year",
                "first_seen_at": now - timedelta(days=2),
                "last_seen_at": now,
                "status": JobListing.Status.ACTIVE,
                "organization": alpha,
            },
        )
        created["active_listings"] = JobListing.objects.filter(
            source=fixture_source
        ).count()

        # -- Password test account with a non-default preference document ----
        user_model = get_user_model()
        user, _ = user_model.objects.get_or_create(
            username=E2E_USERNAME,
            defaults={"email": "e2e_user@example.test", "first_name": "E2E"},
        )
        user.set_password(password)
        user.is_staff = False
        user.save()
        preferences = default_preferences()
        # One non-default dimension: a remote-work preference that the seeded
        # active listing satisfies, so match_jobs returns a live ranked result
        # with a positive work_location factor.
        preferences["work_location"]["modes"] = ["remote"]
        preference, _ = UserPreference.objects.get_or_create(
            user=user,
            defaults={"preferences": preferences},
        )
        if not preference.preferences.get("work_location", {}).get("modes"):
            preference.preferences = preferences
            preference.save(update_fields=["preferences", "modified"])
        created["users"] = 1
        return created
