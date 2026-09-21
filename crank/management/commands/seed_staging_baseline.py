# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Create the staging readiness-baseline fixture set (issue #455, staging-only).

Loads the acceptance fixtures described in
``docs/deployment-baseline-2026-09.md`` into the current database:

- active + superseded company scores (``crank_score`` rows with status 1/0);
- the ``ScoreAlgorithm``/``ScoreAlgorithmWeight`` rows the rankings consumer
  requires (``IndexView`` resolves ``DEFAULT_ALGORITHM_ID`` and its ranking
  SQL inner-joins ``crank_scorealgorithmweight``);
- accepted / stale / conflicting ``CompanyProfileObservation`` evidence, with
  the accepted observation strictly the latest so the ordinary provenance
  surface returns it;
- active / expired ``JobListing`` rows under an approved+enabled source;
- ordinary and unknown-requirement ``JobMatch`` cases for a test account;
- an ordinary test account (throwaway credential, documented in the baseline
  doc) and a staff test account. The staff account is created with an
  **unusable** password and its password is only ever set through an
  explicit ``--staff-password`` provisioning flag, so no privileged
  credential is ever repo-known or reset implicitly.

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
from crank.models.score import Score, ScoreAlgorithm, ScoreAlgorithmWeight, ScoreType
from crank.settings.base import DEFAULT_ALGORITHM_ID

#: Revision of this fixture set. Emitted by ``readiness_baseline`` as the
#: fixture-set revision whenever the fixture markers are present; bump when
#: the fixture rows below change shape or meaning.
FIXTURE_REVISION = "staging-baseline-1"

#: Throwaway staging credential for the *ordinary* (non-privileged) test
#: account only. Never a real credential; documented in the baseline doc.
#: Operators may override with ``--password``. The staff account has no
#: default password at all — see ``--staff-password``.
DEFAULT_ORDINARY_PASSWORD = "staging-baseline-throwaway"

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

#: The conflicted observation is older than the accepted one so the ordinary
#: ``organization_provenance`` surface (latest observation only) returns the
#: accepted row, as the documented ordinary replay claims.
CONFLICTED_OBSERVATION_HOURS_AGO = 1


class Command(BaseCommand):
    help = (
        "Create the staging readiness-baseline fixture set (scores, company "
        "evidence, jobs, matches, test accounts). Dev/staging only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=DEFAULT_ORDINARY_PASSWORD,
            help="Throwaway password for the ordinary staging test account.",
        )
        parser.add_argument(
            "--staff-password",
            default=None,
            help=(
                "Explicitly provision the staff test account's password "
                "(secure operator-supplied value; never a repo default). "
                "Without this flag the staff account is created with an "
                "unusable password and an existing account's password is "
                "never reset."
            ),
        )

    def handle(self, *args, **options):
        env = str(getattr(settings, "ENV", "") or "").strip().lower()
        if env == "prod":
            raise CommandError(
                "seed_staging_baseline refuses to run with ENV=prod; "
                "staging fixtures must never enter production."
            )
        created = self._load(
            password=options["password"],
            staff_password=options["staff_password"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Staging baseline fixtures ready: "
                + ", ".join(f"{key}={value}" for key, value in created.items())
            )
        )

    @transaction.atomic
    def _load(self, *, password: str, staff_password: str | None) -> dict:
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

        # ── Rankings dependencies: default algorithm + its Culture weight ──
        # IndexView resolves DEFAULT_ALGORITHM_ID and its ranking SQL
        # inner-joins crank_scorealgorithmweight, so a fresh database without
        # these rows renders empty rankings regardless of the scores below.
        # get_or_create keeps this idempotent whether or not the repository
        # base seeds (seeds/crank.scorealgorithm*.yaml) were also loaded.
        score_type, _ = ScoreType.objects.get_or_create(name="Culture")
        algorithm, created["algorithm"] = ScoreAlgorithm.objects.get_or_create(
            id=DEFAULT_ALGORITHM_ID,
            defaults={
                "name": "Staging Baseline Algorithm",
                "description_content": "staging-baseline.md",
            },
        )
        _, created["algorithm_weight"] = ScoreAlgorithmWeight.objects.get_or_create(
            algorithm=algorithm,
            type=score_type,
            defaults={"weight": 1.0},
        )

        # ── Active + superseded scores (status 1/0) ──
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
                # Strictly older than the accepted observation so the ordinary
                # provenance surface (latest observation only) returns the
                # accepted row, as the documented ordinary replay claims.
                "observed_at": now - timedelta(hours=CONFLICTED_OBSERVATION_HOURS_AGO),
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
                    # Reserved retention keys (issue #469): the baseline's
                    # expired fixture listing must survive the bounded sweep,
                    # so deletion stays well past the fixture's age.
                    "expiry_days": 30,
                    "deletion_days": 90,
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

        # ── Test accounts ──
        user_model = get_user_model()
        ordinary, created["user_ordinary"] = user_model.objects.get_or_create(
            username=ORDINARY_USERNAME,
            defaults={"email": "staging_user@example.test", "first_name": "Staging"},
        )
        ordinary.set_password(password)
        ordinary.is_staff = False
        ordinary.save()
        # The staff account is privileged (StaffOnlyAdminMixin grants broad
        # admin access from is_staff alone), so it never gets a repo-known
        # default password: it is created with an unusable password and only
        # ever receives one through the explicit --staff-password flag.
        # Re-running without the flag never resets an already-provisioned
        # password.
        staff, created["user_staff"] = user_model.objects.get_or_create(
            username=STAFF_USERNAME,
            defaults={"email": "staging_staff@example.test", "first_name": "Staging"},
        )
        if staff_password is not None:
            staff.set_password(staff_password)
        elif not staff.password or not staff.has_usable_password():
            # Newly created accounts carry an empty password string, which
            # Django still calls "usable"; make it explicitly unusable. An
            # already-provisioned usable password is never reset.
            staff.set_unusable_password()
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
