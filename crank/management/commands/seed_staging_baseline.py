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
- field-level ``CompanyFieldEvidence`` for the example company in every state
  a usability session probes: accepted and fresh (``public_status``),
  accepted but stale (``funding_round``), conflicted (``rto_policy``), and
  one correctable field with no evidence at all (``accelerated_vesting``);
- active / expired ``JobListing`` rows under an approved+enabled source;
- ordinary and unknown-requirement ``JobMatch`` cases for a test account;
- an ordinary test account (throwaway credential, documented in the baseline
  doc) and a staff test account. The staff account is created with an
  **unusable** password and its password is only ever set through an
  explicit ``--staff-password`` provisioning flag, so no privileged
  credential is ever repo-known or reset implicitly.

The command is idempotent (``get_or_create`` everywhere; evidence timestamps
are creation-scoped so a rerun changes nothing, and a ``staging-baseline-1``
database is upgraded in place by adding the evidence rows and the ordinary
match's stored requirements), refuses to run with
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

from crank.agents.jobs.matching import evaluate_requirements, project_criteria
from crank.management.commands.seed_job_sources import SEED_SOURCES
from crank.models.company_profile import CompanyFieldEvidence, CompanyProfileObservation
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.organization import Organization
from crank.models.score import Score, ScoreAlgorithm, ScoreAlgorithmWeight, ScoreType
from crank.models.preference import SCHEMA_VERSION, UserPreference, default_preferences
from crank.services.company_evidence import field_evidence_payload, resolve_field_evidence
from crank.services.match_recompute import RecomputeStatus, recompute_user
from crank.services.preferences import to_markdown
from crank.settings.base import DEFAULT_ALGORITHM_ID

#: Revision of this fixture set. ``readiness_baseline`` reports it when the
#: fixture markers are present and the example company holds field evidence;
#: bump when the fixture rows below change shape or meaning.
FIXTURE_REVISION = "staging-baseline-2"

#: ``ranker_version`` of the matches this command writes; the real matcher
#: never writes it, so ``readiness_baseline`` can tell them apart.
FIXTURE_RANKER_VERSION = "staging-baseline"

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

#: Field-level evidence rows (revision 2) are keyed on this validation version,
#: so a rerun finds them and a ``staging-baseline-1`` database, which has none,
#: gains them. ``public_status`` is the fresh accepted field (180-day window),
#: ``funding_round`` is accepted but verified longer ago than its window,
#: ``rto_policy`` is a conflicted claim awaiting review and
#: ``accelerated_vesting`` deliberately has no evidence.
EVIDENCE_VERSION = "staging-baseline-2"
STALE_EVIDENCE_DAYS = 200

#: What the ordinary match asks of the example company. The stored
#: requirements come from the real evaluator over this document, so which
#: requirement is verified, stale, profile-backed or unknown is decided by the
#: product and the evidence rows above, not written by hand.
MATCH_PREFERENCES = {
    "compensation": {"require_public_company": True},
    "funding_stage": ["public"],
    "work_location": {"modes": ["hybrid"]},
    "vesting": {"prefer_accelerated": True},
}


#: The fixture's evidence fields, in the order the end-of-run reading lists them.
FIXTURE_FIELDS = (
    CompanyFieldEvidence.FieldKey.PUBLIC_STATUS,
    CompanyFieldEvidence.FieldKey.FUNDING_ROUND,
    CompanyFieldEvidence.FieldKey.RTO_POLICY,
    CompanyFieldEvidence.FieldKey.ACCELERATED_VESTING,
)


def fixture_preferences() -> dict:
    """The preference document the ordinary match is evaluated against."""
    preferences = default_preferences()
    preferences["compensation"].update(MATCH_PREFERENCES["compensation"])
    preferences["work_location"].update(MATCH_PREFERENCES["work_location"])
    preferences["vesting"].update(MATCH_PREFERENCES["vesting"])
    preferences["funding_stage"] = list(MATCH_PREFERENCES["funding_stage"])
    return preferences


class Command(BaseCommand):
    help = (
        "Create the staging readiness-baseline fixture set (scores, company "
        "evidence, jobs, matches, test accounts). Dev/staging only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=None,
            help=(
                "Throwaway password for the ordinary staging test account. "
                "Without this flag the account gets the documented default "
                "when it is created, and an existing account keeps its "
                "password."
            ),
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
        notes: list[str] = []
        created, readings = self._load(
            password=options["password"],
            staff_password=options["staff_password"],
            notes=notes,
        )
        self._refresh_citations_of_deleted_rows(notes)
        for note in notes:
            self.stdout.write(note)
        self.stdout.write(
            self.style.SUCCESS(
                "Staging baseline fixtures ready: "
                + ", ".join(f"{key}={value}" for key, value in created.items())
            )
        )
        self.stdout.write(
            "Fixture fields read now: "
            + ", ".join(f"{key}={reading}" for key, reading in readings.items())
        )

    @transaction.atomic
    def _load(
        self, *, password: str | None, staff_password: str | None, notes: list[str]
    ) -> tuple[dict, dict]:
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
        observation_accepted, created["observation_accepted"] = CompanyProfileObservation.objects.get_or_create(
            fingerprint="staging-accepted",
            defaults={
                **obs_defaults,
                "observed_at": now,
                "status": CompanyProfileObservation.Status.ACCEPTED,
                "description": "Fresh accepted profile evidence.",
            },
        )
        observation_stale, created["observation_stale"] = CompanyProfileObservation.objects.get_or_create(
            fingerprint="staging-stale",
            defaults={
                **obs_defaults,
                "source_url": "https://jobs.example.test/profile-old",
                "observed_at": now - timedelta(days=STALE_OBSERVATION_DAYS),
                "status": CompanyProfileObservation.Status.ACCEPTED,
                "description": "Stale evidence: observed_at is far in the past.",
            },
        )
        observation_conflicted, created["observation_conflicted"] = CompanyProfileObservation.objects.get_or_create(
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

        # ── Field-level evidence: fresh, stale, conflicted, and none ──
        # Timestamps live in ``defaults`` (creation-scoped), so a rerun, or an
        # upgrade of a revision-1 database, never rewrites an existing row.
        evidence_url = "https://jobs.example.test/profile"
        field_key = CompanyFieldEvidence.FieldKey
        evidence_state = CompanyFieldEvidence.State
        evidence_rows = (
            (
                "evidence_fresh", field_key.PUBLIC_STATUS, evidence_state.ACCEPTED,
                "Public company", observation_accepted, now, now,
            ),
            (
                "evidence_stale", field_key.FUNDING_ROUND, evidence_state.ACCEPTED,
                "Public", observation_stale,
                now - timedelta(days=STALE_EVIDENCE_DAYS),
                now - timedelta(days=STALE_EVIDENCE_DAYS),
            ),
            (
                "evidence_conflicted", field_key.RTO_POLICY, evidence_state.CONFLICTED,
                "In office", observation_conflicted,
                now - timedelta(hours=CONFLICTED_OBSERVATION_HOURS_AGO), None,
            ),
        )
        for label, key, state, value, observation, observed_at, verified_at in evidence_rows:
            # The lookup has no ``state``: a fixture row that a review moved to
            # another state is still the fixture row, and a field that already
            # holds an accepted fact from another writer keeps that one fact.
            # The review flow keeps one accepted row per field; nothing here
            # may add a second or undo a decision.
            field_rows = CompanyFieldEvidence.objects.filter(
                organization=target_org, field_key=key
            )
            own = field_rows.filter(validation_version=EVIDENCE_VERSION).first()
            if own is not None:
                created[label] = False
                if own.state != state:
                    notes.append(
                        f"Kept the existing {key} evidence row (now {own.state}); "
                        "the fixture state was not restored."
                    )
                continue
            if field_rows.filter(state=evidence_state.ACCEPTED).exists():
                created[label] = False
                notes.append(
                    f"Kept the existing accepted {key} evidence row; "
                    "no fixture row was added."
                )
                continue
            CompanyFieldEvidence.objects.create(
                organization=target_org,
                field_key=key,
                state=state,
                validation_version=EVIDENCE_VERSION,
                value_text=value,
                source_url=evidence_url,
                source_domain="jobs.example.test",
                observation=observation,
                observed_at=observed_at,
                extractor_version=EVIDENCE_VERSION,
                last_checked_at=observed_at,
                last_verified_at=verified_at,
            )
            created[label] = True

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
                # Linked, so the real matcher considers the listing for the
                # example company (a listing with no organization is excluded).
                "organization": target_org,
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
        # Link an earlier fixture listing that has no organization; a listing
        # that points at another organization is never relinked.
        if active_listing.organization_id is None:
            active_listing.organization = target_org
            active_listing.save(update_fields=["organization", "modified"])
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
        # The password is set when the account is created or when the operator
        # passes --password on this run; a rerun never signs the participant
        # out or replaces a password the operator chose.
        if created["user_ordinary"] or password is not None:
            ordinary.set_password(password or DEFAULT_ORDINARY_PASSWORD)
        ordinary.is_staff = False
        ordinary.save()
        # The staff account is privileged (StaffOnlyAdminMixin grants broad
        # admin access from is_staff alone), so it never keeps a repo-known
        # password: it is created with an unusable password and only ever
        # receives one through the explicit --staff-password flag. A rerun
        # without the flag leaves an unusable or operator-set password alone
        # and replaces one the repository knows (empty or the ordinary
        # default) with an unusable value.
        staff, created["user_staff"] = user_model.objects.get_or_create(
            username=STAFF_USERNAME,
            defaults={"email": "staging_staff@example.test", "first_name": "Staging"},
        )
        if staff_password is not None:
            staff.set_password(staff_password)
        elif (
            not staff.password
            or staff.has_usable_password()
            and staff.check_password(DEFAULT_ORDINARY_PASSWORD)
        ):
            # Newly created accounts carry an empty password string, which
            # Django still calls "usable"; make it explicitly unusable.
            staff.set_unusable_password()
        staff.is_staff = True
        staff.save()

        # ── Saved priorities for the ordinary account ──
        # Without a saved preference document the product serves no match to
        # this account and a refresh publishes none. Created only when the
        # account has none: a participant's or moderator's saved priorities
        # are never overwritten.
        preferences = fixture_preferences()
        # A recreated row starts above the revision of the results already
        # published for the account, or its next refresh is discarded as stale.
        published_revision = (
            MatchResultState.objects.filter(user=ordinary)
            .values_list("preference_revision", flat=True)
            .first()
        )
        preference, created["preference_ordinary"] = UserPreference.objects.get_or_create(
            user=ordinary,
            defaults={
                "preferences": preferences,
                "preferences_markdown": to_markdown(preferences),
                "schema_version": SCHEMA_VERSION,
                "revision": 0 if published_revision is None else published_revision + 1,
            },
        )
        if preference.preferences != preferences:
            notes.append(
                f"Kept the saved priorities of {ORDINARY_USERNAME}; they differ "
                "from the fixture's, so the stored match no longer matches what a "
                "refresh would publish."
            )

        # ── Ordinary + unknown-requirement match cases ──
        # The ordinary match stores the requirements the real evaluator
        # reports for this listing and company, so it carries a requirement
        # confirmed by the fresh evidence row, one resting on stale evidence,
        # and the unknown / profile-backed cases.
        evidence = resolve_field_evidence(target_org)
        outcomes = evaluate_requirements(
            active_listing,
            project_criteria(preferences, SCHEMA_VERSION),
            organization=target_org,
            evidence=evidence,
        )
        requirements = [outcome.as_dict() for outcome in outcomes]
        evidence_ids = sorted(
            {o.source_id for o in outcomes if o.source_kind == "evidence"}
        )
        match_ordinary, created["match_ordinary"] = JobMatch.objects.get_or_create(
            user=ordinary,
            listing=active_listing,
            preference_version=1,
            ranker_version=FIXTURE_RANKER_VERSION,
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
                "requirements": requirements,
                "evidence_ids": evidence_ids,
                "first_matched_at": now - timedelta(days=1),
                "last_matched_at": now,
            },
        )
        # Upgrade a revision-1 match in place; a no-op when already current.
        if (match_ordinary.requirements, match_ordinary.evidence_ids) != (
            requirements,
            evidence_ids,
        ):
            match_ordinary.requirements = requirements
            match_ordinary.evidence_ids = evidence_ids
            match_ordinary.save(
                update_fields=["requirements", "evidence_ids", "modified"]
            )
            notes.append(
                "Rewrote the stored requirements of the ordinary match "
                f"({ORDINARY_USERNAME}, preference_version 1) to what the evidence "
                "reads now."
            )
        _, created["match_unknown_requirement"] = JobMatch.objects.get_or_create(
            user=ordinary,
            listing=active_listing,
            preference_version=2,
            ranker_version=FIXTURE_RANKER_VERSION,
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

        readings = self._readings(target_org)
        return created, readings

    @staticmethod
    def _refresh_citations_of_deleted_rows(notes: list[str]) -> None:
        """Republish the account's matches when one cites a deleted evidence row.

        The documented reset deletes the example company's evidence rows. A
        match the real matcher already published for the account then cites
        rows that are gone, and a scheduled refresh answers ``current`` because
        this command records no change. One forced refresh, the product's own
        path, makes the published match cite the rows just written.
        """
        user = get_user_model().objects.filter(username=ORDINARY_USERNAME).first()
        if user is None:
            return
        cited = set()
        for ids in (
            JobMatch.objects.filter(user=user)
            .exclude(ranker_version=FIXTURE_RANKER_VERSION)
            .values_list("evidence_ids", flat=True)
        ):
            cited.update(ids or [])
        existing = set(
            CompanyFieldEvidence.objects.filter(pk__in=cited).values_list("pk", flat=True)
        )
        if cited <= existing:
            return
        outcome = recompute_user(user, reason="seed_staging_baseline", force=True)
        if outcome.status == RecomputeStatus.PUBLISHED:
            notes.append(
                f"Republished the matches of {ORDINARY_USERNAME}: one cited an "
                "evidence row that no longer exists."
            )
        else:
            notes.append(
                f"A published match of {ORDINARY_USERNAME} cites an evidence row "
                f"that no longer exists and the refresh answered {outcome.status.value}; "
                "it is not repaired."
            )

    @staticmethod
    def _readings(organization) -> dict[str, str]:
        """What each fixture field reads at this moment, by the product's rules."""
        payload = field_evidence_payload(organization)
        fields = {row["field_key"]: row for row in payload["fields"]}
        readings = {}
        for key in FIXTURE_FIELDS:
            row = fields.get(key)
            if row is not None:
                reading = row["status"]
            elif payload["review_by_field"].get(key) == "conflicted":
                reading = "conflicted"
            else:
                reading = "unknown"
            readings[key] = reading
        return readings
