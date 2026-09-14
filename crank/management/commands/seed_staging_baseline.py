# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Create the staging readiness-baseline fixture set (issue #455, staging-only).

Loads the acceptance fixtures described in
``docs/deployment-baseline-2026-09.md`` into the current database:

- active + superseded company scores (``crank_score`` rows with status 1/0);
- accepted / stale / conflicting ``CompanyProfileObservation`` evidence;
- active / expired ``JobListing`` rows under an approved+enabled source;
- ordinary and unknown-requirement ``JobMatch`` cases for a test account;
- ordinary and staff test accounts (throwaway credentials, never real ones).

The command is idempotent (``get_or_create`` everywhere), refuses to run with
``ENV=prod``, and never touches approval policy of pre-existing sources other
than creating its own approved+enabled fixture source.

Reproducing the September 12 audit failures (zero approved/enabled sources, no
latest run) is the *empty* state: run ``seed_job_sources`` only and then
``readiness_baseline`` — see the baseline document for the exact procedure.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crank.management.commands.seed_job_sources import SEED_SOURCES
from crank.models.company_profile import CompanyProfileObservation
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.organization import Organization
from crank.models.score import Score, ScoreType

#: Throwaway staging credentials. Never a real credential; documented in the
#: baseline doc. Operators may override with ``--password``.
DEFAULT_TEST_PASSWORD = "staging-baseline-throwaway"

ORDINARY_USERNAME = "staging_user"
STAFF_USERNAME = "staging_staff"

#: Fixture organization names (unique keys make the command idempotent).
TARGET_ORG_NAME = "Staging Example Corp"
RATING_SOURCE_ORG_NAME = "Staging Rating Source"

#: Fixture source name must be distinct from the curated ``seed_job_sources``
#: rows so the staging fixture never rewrites curated approval policy.
FIXTURE_SOURCE_NAME = "Staging Baseline Source"

STALE_OBSERVATION_DAYS = 45
EXPIRED_LISTING_DAYS = 30


class Command(BaseCommand):
    help = (
        "Create the staging readiness-baseline fixture set (scores, company "
        "evidence, jobs, matches, test accounts). Dev/staging only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=DEFAULT_TEST_PASSWORD,
            help="Throwaway password for both staging test accounts.",
        )

    def handle(self, *args, **options):
        env = str(getattr(settings, "ENV", "") or "").strip().lower()
        if env == "prod":
            raise CommandError(
                "seed_staging_baseline refuses to run with ENV=prod; "
                "staging fixtures must never enter production."
            )
        created = self._load(password=options["password"])
        self.stdout.write(
            self.style.SUCCESS(
                "Staging baseline fixtures ready: "
                + ", ".join(f"{key}={value}" for key, value in created.items())
            )
        )

    @transaction.atomic
    def _load(self, *, password: str) -> dict:
        now = timezone.now()
        created: dict[str, int] = {}

        # ── Companies ──
        target_org, created["target_org"] = Organization.objects.get_or_create(
            name=TARGET_ORG_NAME,
            defaults={
                "type": Organization.Type.COMPANY,
                "url": "https://jobs.example.test/",
                "gives_ratings": False,
                "public": True,
                "funding_round": Organization.FundingRound.PUBLIC,
                "rto_policy": Organization.RTOPolicy.HYBRID,
            },
        )
        rating_source_org, created["rating_source_org"] = Organization.objects.get_or_create(
            name=RATING_SOURCE_ORG_NAME,
            defaults={
                "type": Organization.Type.COMPANY,
                "url": "https://jobs.example.test/ratings",
                "gives_ratings": True,
                "public": True,
                "funding_round": Organization.FundingRound.PUBLIC,
                "rto_policy": Organization.RTOPolicy.HYBRID,
            },
        )

        # ── Active + superseded scores (status 1/0) ──
        score_type, _ = ScoreType.objects.get_or_create(name="Culture")
        _, created["score_superseded"] = Score.objects.get_or_create(
            type=score_type,
            source=rating_source_org,
            target=target_org,
            status=0,
            defaults={
                "score": 3.0,
                "low_threshold": 0.0,
                "high_threshold": 5.0,
                "activate_date": now - timedelta(days=60),
                "deactivate_date": now - timedelta(days=30),
            },
        )
        _, created["score_active"] = Score.objects.get_or_create(
            type=score_type,
            source=rating_source_org,
            target=target_org,
            status=1,
            defaults={
                "score": 4.2,
                "low_threshold": 0.0,
                "high_threshold": 5.0,
                "activate_date": now - timedelta(days=30),
                "deactivate_date": None,
            },
        )

        # ── Accepted / stale / conflicting company evidence ──
        obs_defaults = dict(
            organization=target_org,
            source_url="https://jobs.example.test/profile",
            observed_domain="jobs.example.test",
            observed_name=TARGET_ORG_NAME,
            extraction_version="staging-baseline-1",
        )
        _, created["observation_accepted"] = CompanyProfileObservation.objects.get_or_create(
            fingerprint="staging-accepted",
            defaults={
                **obs_defaults,
                "observed_at": now,
                "status": CompanyProfileObservation.Status.ACCEPTED,
                "description": "Fresh accepted profile evidence.",
            },
        )
        _, created["observation_stale"] = CompanyProfileObservation.objects.get_or_create(
            fingerprint="staging-stale",
            defaults={
                **obs_defaults,
                "source_url": "https://jobs.example.test/profile-old",
                "observed_at": now - timedelta(days=STALE_OBSERVATION_DAYS),
                "status": CompanyProfileObservation.Status.ACCEPTED,
                "description": "Stale evidence: observed_at is far in the past.",
            },
        )
        _, created["observation_conflicted"] = CompanyProfileObservation.objects.get_or_create(
            fingerprint="staging-conflicted",
            defaults={
                **obs_defaults,
                "source_url": "https://jobs.example.test/profile-conflict",
                "observed_at": now,
                "status": CompanyProfileObservation.Status.CONFLICTED,
                "conflict_fields": ["rto_evidence"],
                "rto_evidence": "One source says remote; another says in-office.",
            },
        )

        # ── Approved+enabled fixture source with active/expired listings ──
        usajobs = next(
            entry
            for entry in SEED_SOURCES
            if entry["adapter_key"] == "usajobs"
        )
        fixture_source, created["fixture_source"] = JobSourceCatalog.objects.get_or_create(
            name=FIXTURE_SOURCE_NAME,
            defaults={
                "adapter_key": usajobs["adapter_key"],
                "base_url": usajobs["base_url"],
                "approval_state": JobSourceCatalog.ApprovalState.APPROVED,
                "enabled": True,
                "catalog_metadata": {
                    "description": "Staging baseline fixture source (approved+enabled).",
                },
            },
        )
        active_listing, created["listing_active"] = JobListing.all_objects.get_or_create(
            source=fixture_source,
            canonical_url="https://www.usajobs.gov/Job/staging-baseline-active",
            defaults={
                "external_id": "staging-active-1",
                "employer_name": TARGET_ORG_NAME,
                "employer_domain": "jobs.example.test",
                "title": "Staging Baseline Active Role",
                "location_text": "Remote",
                "is_remote": True,
                "compensation_min": 120000,
                "compensation_max": 160000,
                "compensation_currency": "USD",
                "compensation_interval": "year",
                "first_seen_at": now - timedelta(days=2),
                "last_seen_at": now,
                "status": JobListing.Status.ACTIVE,
            },
        )
        _, created["listing_expired"] = JobListing.all_objects.get_or_create(
            source=fixture_source,
            canonical_url="https://www.usajobs.gov/Job/staging-baseline-expired",
            defaults={
                "external_id": "staging-expired-1",
                "employer_name": TARGET_ORG_NAME,
                "employer_domain": "jobs.example.test",
                "title": "Staging Baseline Expired Role",
                "location_text": "Hybrid",
                "is_remote": False,
                "first_seen_at": now - timedelta(days=60),
                "last_seen_at": now - timedelta(days=EXPIRED_LISTING_DAYS),
                "status": JobListing.Status.EXPIRED,
            },
        )

        # ── Test accounts (throwaway) ──
        user_model = get_user_model()
        ordinary, created["user_ordinary"] = user_model.objects.get_or_create(
            username=ORDINARY_USERNAME,
            defaults={"email": "staging_user@example.test", "first_name": "Staging"},
        )
        ordinary.set_password(password)
        ordinary.is_staff = False
        ordinary.save()
        staff, created["user_staff"] = user_model.objects.get_or_create(
            username=STAFF_USERNAME,
            defaults={"email": "staging_staff@example.test", "first_name": "Staging"},
        )
        staff.set_password(password)
        staff.is_staff = True
        staff.save()

        # ── Ordinary + unknown-requirement match cases ──
        _, created["match_ordinary"] = JobMatch.objects.get_or_create(
            user=ordinary,
            listing=active_listing,
            preference_version=1,
            ranker_version="staging-baseline",
            defaults={
                "organization": target_org,
                "score": 4.0,
                "factors": [
                    {
                        "factor": "organization_scores",
                        "score": 1.5,
                        "detail": "average=4.2/5 (type: Culture)",
                    },
                    {
                        "factor": "compensation",
                        "score": 1.0,
                        "detail": "salary_min=120000 USD/year",
                    },
                ],
                "first_matched_at": now - timedelta(days=1),
                "last_matched_at": now,
            },
        )
        _, created["match_unknown_requirement"] = JobMatch.objects.get_or_create(
            user=ordinary,
            listing=active_listing,
            preference_version=2,
            ranker_version="staging-baseline",
            defaults={
                "organization": target_org,
                "score": 2.0,
                # An unknown-requirement case: the factor names a requirement
                # that has no JobCriteria projection. Matching/UI code must
                # treat it as neutral data rather than crashing.
                "factors": [
                    {
                        "factor": "requirement:quantum_fluency",
                        "score": 0,
                        "detail": "unknown requirement; no criteria projection",
                    }
                ],
                "first_matched_at": now - timedelta(days=1),
                "last_matched_at": now,
            },
        )
        return created
