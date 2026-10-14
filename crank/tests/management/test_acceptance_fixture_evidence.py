# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Acceptance fixtures carry field-level evidence (issue #492, AC 17, 18).

The invariant: in both fixture sets (``seed_staging_baseline`` and
``seed_e2e``) every match requirement that the real matching path reports as
confirmed (the status the product calls "Verified") resolves to an existing,
accepted ``CompanyFieldEvidence`` row of the matched organization. The status
rule is the product's own (``annotate_requirement_evidence``); nothing here
re-implements it, and nothing pins a ``seed_e2e`` row count.
"""

import io
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
from django.test import TestCase, override_settings

from crank.management.commands.seed_staging_baseline import (
    DEFAULT_ORDINARY_PASSWORD,
    EVIDENCE_VERSION,
    fixture_preferences,
    ORDINARY_USERNAME,
    TARGET_ORG_NAME,
)
from crank.agents.jobs.matching import project_criteria
from crank.models.company_correction import CompanyCorrection
from crank.models.company_profile import CompanyFieldEvidence, CompanyProfileObservation
from crank.models.job import JobListing
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.organization import Organization
from crank.models.preference import SCHEMA_VERSION, UserPreference, default_preferences
from crank.services.preferences import (
    StalePreferenceError,
    apply_patch_to_user,
    to_markdown,
    undo_preference_change,
)
from crank.services import company_evidence
from crank.services.company_evidence import resolve_field_evidence_for_orgs
from crank.services.job_matching import evaluate_org_requirements
from crank.services.match_recompute import RecomputeStatus, recompute_user

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
FieldKey = CompanyFieldEvidence.FieldKey
State = CompanyFieldEvidence.State


def _seed(command):
    call_command(command, stdout=io.StringIO(), no_color=True)


def _broad_preferences():
    """A preference document that sets every requirement evidence can state."""
    preferences = default_preferences()
    preferences["compensation"]["require_public_company"] = True
    preferences["funding_stage"] = [
        code.casefold() for code, _ in Organization.FundingRound.choices
    ]
    preferences["work_location"]["modes"] = ["remote", "hybrid", "in-office"]
    preferences["work_location"]["max_in_office_days"] = 0
    preferences["vesting"]["prefer_accelerated"] = True
    return preferences


def _annotated_requirements(user):
    """``(organization_id, annotated requirement)`` pairs from the real paths.

    The stored requirements of the user's ``JobMatch`` rows, and the
    requirements ``evaluate_org_requirements`` reports for every organization
    against a broad preference document (hard filters would drop the very
    companies being checked), each annotated with the read-time evidence
    status the views use.
    """
    stored = list(JobMatch.objects.filter(user=user))
    criteria = project_criteria(_broad_preferences(), SCHEMA_VERSION)
    organizations = list(Organization.objects.order_by("pk"))
    evidence = resolve_field_evidence_for_orgs([org.pk for org in organizations])
    organization_ids = [match.organization_id for match in stored] + [
        org.pk for org in organizations
    ]
    annotated = company_evidence.annotate_requirement_evidence(
        [match.requirements for match in stored]
        + [
            [
                outcome.as_dict()
                for outcome in evaluate_org_requirements(
                    org, criteria, evidence.get(org.pk)
                )
            ]
            for org in organizations
        ]
    )
    return [
        (organization_id, requirement)
        for organization_id, requirements in zip(organization_ids, annotated, strict=True)
        for requirement in requirements
    ]


def _confirmed(pairs):
    return [
        (organization_id, requirement)
        for organization_id, requirement in pairs
        if (requirement.get("evidence_status") or {}).get("state") == "verified"
    ]


class AcceptanceFixtureEvidenceTests(TestCase):
    def _assert_confirmed_requirements_are_backed(self, user, *, require_confirmed=True):
        pairs = _annotated_requirements(user)
        confirmed = _confirmed(pairs)
        if require_confirmed:
            self.assertTrue(
                confirmed,
                "the fixture set yields no confirmed requirement; the check would be vacuous",
            )
        else:
            # The loop must still examine real evidence-backed requirements.
            self.assertTrue(
                [r for _, r in pairs if r.get("source_kind") == "evidence"],
                "the fixture set yields no evidence-backed requirement at all",
            )
        for organization_id, requirement in confirmed:
            self.assertEqual(requirement["source_kind"], "evidence", requirement)
            # Resolve the cited row the way the product does: a superseded row
            # re-accepted with the same value and scope reads as that accepted row.
            _, accepted = company_evidence._evidence_rows_and_statuses(
                [requirement["source_id"]], None
            )
            row = accepted.get(requirement["source_id"])
            self.assertIsNotNone(row, f"no evidence row for confirmed {requirement}")
            self.assertEqual(row.state, State.ACCEPTED, requirement)
            self.assertEqual(row.organization_id, organization_id, requirement)

    def test_staging_baseline_confirmed_requirements_have_accepted_evidence(self):
        _seed("seed_staging_baseline")
        user = get_user_model().objects.get(username=ORDINARY_USERNAME)
        self._assert_confirmed_requirements_are_backed(user)

    @override_settings(CACHES=LOCMEM)
    def test_seed_e2e_confirmed_requirements_have_accepted_evidence(self):
        from crank.management.commands.seed_e2e import E2E_USERNAME

        _seed("seed_e2e")
        user = get_user_model().objects.get(username=E2E_USERNAME)
        # The e2e user has saved preferences and no stored match: run the real
        # matcher for that user and check the rows it stores, as the staging
        # half does. The status rule is the product's, not re-implemented here.
        outcome = recompute_user(user, reason="test", force=True)
        self.assertEqual(outcome.status, RecomputeStatus.PUBLISHED)
        self.assertTrue(JobMatch.objects.filter(user=user).exists())
        # seed_e2e's accepted evidence is prose ("Remote-first"), long past its
        # window (beta) or off-domain, so today none of it is confirmed, and this
        # half cannot require a confirmed requirement until seed_e2e gets one
        # fresh, accepted, company-wide row (issue #582). The invariant holds for
        # whatever it does confirm; nothing pins its rows or that it stays empty.
        self._assert_confirmed_requirements_are_backed(user, require_confirmed=False)


class StagingBaselineEvidenceStatesTests(TestCase):
    """AC 17: the example company carries each evidence state a session probes."""

    def _statuses(self):
        match = JobMatch.objects.get(
            user__username=ORDINARY_USERNAME, preference_version=1
        )
        annotated = company_evidence.annotate_requirement_evidence([match.requirements])[0]
        return {r["path"]: (r.get("evidence_status") or {}).get("state") for r in annotated}

    def test_each_state_is_present_and_the_match_reports_it(self):
        _seed("seed_staging_baseline")
        org = Organization.objects.get(name=TARGET_ORG_NAME)
        rows = {
            (row.field_key, row.state): row
            for row in CompanyFieldEvidence.objects.filter(organization=org)
        }
        self.assertEqual(
            set(rows),
            {
                (FieldKey.PUBLIC_STATUS, State.ACCEPTED),
                (FieldKey.FUNDING_ROUND, State.ACCEPTED),
                (FieldKey.RTO_POLICY, State.CONFLICTED),
            },
        )
        self.assertFalse(
            CompanyFieldEvidence.objects.filter(
                organization=org, field_key=FieldKey.ACCELERATED_VESTING
            ).exists(),
            "the unknown case must have no evidence",
        )
        self.assertEqual(
            company_evidence.field_status(rows[(FieldKey.PUBLIC_STATUS, State.ACCEPTED)]),
            "verified",
        )
        self.assertEqual(
            company_evidence.field_status(rows[(FieldKey.FUNDING_ROUND, State.ACCEPTED)]),
            "stale",
        )
        statuses = self._statuses()
        self.assertEqual(statuses["compensation.require_public_company"], "verified")
        self.assertEqual(statuses["funding_stage"], "stale")
        # The work-mode requirement is a MISMATCH decided by the listing's own
        # ``is_remote`` flag (no evidence state), and the vesting requirement a
        # MISMATCH from the model default, shown as profile data. The conflicted
        # claim and the unknown field are not in the match; they show in the
        # company view's evidence.
        self.assertIsNone(statuses["work_location.modes"])
        self.assertEqual(statuses["vesting.prefer_accelerated"], "profile")
        self.assertEqual(len(statuses), 4)
        payload = company_evidence.field_evidence_payload(org)
        self.assertIn(FieldKey.ACCELERATED_VESTING, payload["unverified_fields"])

    def test_rerun_changes_nothing(self):
        _seed("seed_staging_baseline")
        before = self._snapshot()
        _seed("seed_staging_baseline")
        self.assertEqual(self._snapshot(), before)

    def test_revision_1_database_is_upgraded_in_place(self):
        _seed("seed_staging_baseline")
        # Rebuild the revision-1 shape: no field evidence, a match with
        # factors only.
        CompanyFieldEvidence.objects.filter(validation_version=EVIDENCE_VERSION).delete()
        JobMatch.objects.update(requirements=[], evidence_ids=[])
        pks_before = set(JobMatch.objects.values_list("pk", flat=True))

        _seed("seed_staging_baseline")

        self.assertEqual(set(JobMatch.objects.values_list("pk", flat=True)), pks_before)
        upgraded = self._snapshot()
        self.assertEqual(len(upgraded["evidence"]), 3)
        self.assertEqual(
            JobMatch.objects.get(preference_version=1).evidence_ids,
            sorted(
                CompanyFieldEvidence.objects.filter(state=State.ACCEPTED).values_list(
                    "pk", flat=True
                )
            ),
        )
        self.assertEqual(self._statuses()["compensation.require_public_company"], "verified")
        _seed("seed_staging_baseline")
        self.assertEqual(self._snapshot(), upgraded)

    @staticmethod
    def _snapshot():
        evidence = sorted(
            (
                row.pk, row.field_key, row.state, row.value_text, row.observed_at,
                row.last_verified_at, row.observation_id,
            )
            for row in CompanyFieldEvidence.objects.all()
        )
        matches = sorted(
            (m.pk, m.score, str(m.factors), str(m.requirements), str(m.evidence_ids))
            for m in JobMatch.objects.all()
        )
        return {"evidence": evidence, "matches": matches}


def _run(*, now=None, **options):
    """Run the staging command; return its output lines."""
    out = io.StringIO()
    if now is None:
        call_command("seed_staging_baseline", stdout=out, no_color=True, **options)
    else:
        with mock.patch("django.utils.timezone.now", return_value=now):
            call_command("seed_staging_baseline", stdout=out, no_color=True, **options)
    return out.getvalue().splitlines()


def _org():
    return Organization.objects.get(name=TARGET_ORG_NAME)


def _staff():
    return get_user_model().objects.get_or_create(username="reviewer", defaults={"is_staff": True})[0]


def _rows(**filters):
    return CompanyFieldEvidence.objects.filter(organization=_org(), **filters)


def _ordinary_match():
    return JobMatch.objects.get(
        user__username=ORDINARY_USERNAME, preference_version=1, ranker_version="staging-baseline"
    )


class ReviewedDatabaseRerunTests(TestCase):
    """A second run after a review action never adds a row or undoes a decision."""

    def setUp(self):
        _run()

    def _second_run(self):
        before = set(CompanyFieldEvidence.objects.values_list("pk", flat=True))
        lines = _run()
        self.assertEqual(
            set(CompanyFieldEvidence.objects.values_list("pk", flat=True)), before
        )
        accepted = {}
        for row in _rows(state=State.ACCEPTED):
            accepted.setdefault(row.field_key, []).append(row.pk)
        self.assertTrue(all(len(pks) == 1 for pks in accepted.values()), accepted)
        return lines

    def _match_reads(self):
        match = _ordinary_match()
        resolved = company_evidence.resolve_field_evidence(_org())
        self.assertEqual(
            match.evidence_ids,
            sorted(
                r["source_id"] for r in match.requirements if r["source_kind"] == "evidence"
            ),
        )
        self.assertTrue(set(match.evidence_ids) <= {row.pk for row in resolved.values()})
        return match, company_evidence.annotate_requirement_evidence([match.requirements])[0]

    def _correct_public_status(self, value="Private company"):
        current = _rows(field_key=FieldKey.PUBLIC_STATUS, state=State.ACCEPTED).get()
        correction = CompanyCorrection.objects.create(
            requester=get_user_model().objects.get(username=ORDINARY_USERNAME),
            organization=_org(),
            field_key=FieldKey.PUBLIC_STATUS,
            current_value=current.value_text,
            current_evidence=current,
            proposed_value=value,
            evidence_url="https://corrections.example.org/proof",
            scope_level=CompanyCorrection.ScopeLevel.COMPANY,
            idempotency_key=uuid.uuid4(),
        )
        return current, company_evidence.accept_correction(correction, reviewer=_staff())

    def test_r1_accepted_correction_is_kept(self):
        current, accepted = self._correct_public_status()
        current.refresh_from_db()
        self.assertEqual(current.state, State.SUPERSEDED)
        lines = self._second_run()
        self.assertEqual(
            _rows(field_key=FieldKey.PUBLIC_STATUS, state=State.ACCEPTED).get().pk, accepted.pk
        )
        self.assertTrue(any("public_status" in line and "kept" in line.lower() for line in lines))
        match, annotated = self._match_reads()
        # The match cites the staff decision, not a new fixture row.
        self.assertIn(accepted.pk, match.evidence_ids)
        self.assertNotIn(current.pk, match.evidence_ids)
        self.assertNotEqual(
            {r["path"]: r["status"] for r in annotated}["compensation.require_public_company"],
            "match",
        )

    def test_r1_correction_with_the_same_value_keeps_the_match_verified(self):
        _, accepted = self._correct_public_status(value="Public company")
        self._second_run()
        _, annotated = self._match_reads()
        statuses = {r["path"]: (r["evidence_status"] or {}).get("state") for r in annotated}
        self.assertEqual(statuses["compensation.require_public_company"], "verified")
        self.assertIn(accepted.pk, _ordinary_match().evidence_ids)

    def test_r2_accepted_conflicted_claim_is_kept(self):
        claim = _rows(field_key=FieldKey.RTO_POLICY, state=State.CONFLICTED).get()
        accepted = company_evidence.accept_claim(claim, reviewer=_staff())
        lines = self._second_run()
        self.assertEqual(
            _rows(field_key=FieldKey.RTO_POLICY, state=State.ACCEPTED).get().pk, accepted.pk
        )
        self.assertTrue(any("rto_policy" in line for line in lines))
        self._match_reads()

    def test_r3_rejected_claim_stays_rejected_once_and_twice(self):
        claim = _rows(field_key=FieldKey.RTO_POLICY, state=State.CONFLICTED).get()
        company_evidence.reject_claim(claim, reviewer=_staff())
        lines = self._second_run()
        self.assertTrue(any("rto_policy" in line for line in lines))
        self.assertFalse(_rows(field_key=FieldKey.RTO_POLICY, state=State.ACCEPTED).exists())
        self.assertEqual(_rows(field_key=FieldKey.RTO_POLICY).get().state, State.REJECTED)
        self._second_run()
        self._match_reads()

    def test_r4_accepted_rows_of_another_validation_version_are_kept(self):
        observation = CompanyProfileObservation.objects.get(fingerprint="staging-accepted")
        _rows().delete()
        now = timezone.now()
        other = {}
        for key, value in (
            (FieldKey.PUBLIC_STATUS, "Public company"),
            (FieldKey.FUNDING_ROUND, "Series C"),
            (FieldKey.RTO_POLICY, "Remote"),
        ):
            other[key] = CompanyFieldEvidence.objects.create(
                organization=_org(), field_key=key, state=State.ACCEPTED, value_text=value,
                source_url="https://jobs.example.test/profile", source_domain="jobs.example.test",
                observation=observation, observed_at=now, validation_version="company-profile.v3",
                extractor_version="company-profile.v3", last_checked_at=now, last_verified_at=now,
            )
        lines = self._second_run()
        self.assertEqual(
            {row.field_key: row.pk for row in _rows(state=State.ACCEPTED)},
            {key: row.pk for key, row in other.items()},
        )
        self.assertEqual(sum("Kept the existing accepted" in line for line in lines), 3)
        match, _ = self._match_reads()
        # The match reads the other writer's rows, never a fixture row.
        self.assertTrue(match.evidence_ids)
        self.assertLessEqual(set(match.evidence_ids), {row.pk for row in other.values()})
        self.assertIn(other[FieldKey.PUBLIC_STATUS].pk, match.evidence_ids)

    def test_r5_a_recheck_that_made_the_stale_row_fresh_is_not_touched(self):
        company_evidence.record_check(_org(), FieldKey.FUNDING_ROUND, success=True, verified=True)
        row = _rows(field_key=FieldKey.FUNDING_ROUND).get()
        verified_at = row.last_verified_at
        self.assertEqual(company_evidence.field_status(row), "verified")
        self._second_run()
        row.refresh_from_db()
        self.assertEqual(row.last_verified_at, verified_at)
        _, annotated = self._match_reads()
        statuses = {r["path"]: (r["evidence_status"] or {}).get("state") for r in annotated}
        self.assertEqual(statuses["funding_stage"], "verified")

    def test_untouched_second_run_is_quiet(self):
        lines = _run()
        self.assertFalse([line for line in lines if "kept" in line.lower() or "Rewrote" in line])

    def test_reset_procedure_gives_back_the_fresh_states(self):
        fresh = sorted(
            (row.field_key, row.state, row.value_text) for row in _rows()
        )
        fresh_reads = self._statuses()
        self._correct_public_status()
        company_evidence.accept_claim(
            _rows(field_key=FieldKey.RTO_POLICY, state=State.CONFLICTED).get(), reviewer=_staff()
        )
        _run()
        self.assertNotEqual(sorted((r.field_key, r.state, r.value_text) for r in _rows()), fresh)
        _rows().delete()
        _run()
        self.assertEqual(sorted((r.field_key, r.state, r.value_text) for r in _rows()), fresh)
        self.assertEqual(self._statuses(), fresh_reads)

    def _statuses(self):
        _, annotated = self._match_reads()
        return {r["path"]: (r["evidence_status"] or {}).get("state") for r in annotated}


class StagingAgingTests(TestCase):
    def test_fixtures_that_aged_are_reported_and_the_remedy_works(self):
        start = timezone.now()
        first = _run()
        self.assertIn(
            "Fixture fields read now: public_status=verified, funding_round=stale, "
            "rto_policy=conflicted, accelerated_vesting=unknown",
            first,
        )
        later = start + timedelta(days=181)
        before = set(CompanyFieldEvidence.objects.values_list("pk", flat=True))
        lines = _run(now=later)
        self.assertEqual(set(CompanyFieldEvidence.objects.values_list("pk", flat=True)), before)
        reading = [line for line in lines if line.startswith("Fixture fields read now")][0]
        self.assertIn("public_status=stale", reading)
        annotated = company_evidence.annotate_requirement_evidence(
            [_ordinary_match().requirements], now=later
        )[0]
        self.assertFalse(
            [r for r in annotated if (r["evidence_status"] or {}).get("state") == "verified"]
        )
        # The documented remedy: delete the fixture row and run again.
        _rows(field_key=FieldKey.PUBLIC_STATUS).delete()
        lines = _run(now=later)
        self.assertTrue(
            any(line.startswith("Fixture fields read now: public_status=verified") for line in lines)
        )
        new = _rows(field_key=FieldKey.PUBLIC_STATUS, state=State.ACCEPTED).get()
        self.assertIn(new.pk, _ordinary_match().evidence_ids)


class EvidenceStatusRuleTests(TestCase):
    def test_a_requirement_citing_a_non_accepted_row_never_reads_verified(self):
        _run()
        claim = _rows(field_key=FieldKey.RTO_POLICY).get()
        # The shape the product stores for the work-mode requirement.
        requirement = {
            "path": "work_location.modes", "status": "mismatch", "scope_ok": True,
            "source_kind": "evidence", "source_id": claim.pk, "observed": "in-office",
        }

        def read(state):
            CompanyFieldEvidence.objects.filter(pk=claim.pk).update(
                state=state, last_verified_at=timezone.now()
            )
            annotated = company_evidence.annotate_requirement_evidence([[requirement]])
            return annotated[0][0]["evidence_status"]["state"]

        # Positive control: the same requirement reads verified once the row is
        # the accepted, fresh fact.
        self.assertEqual(read(State.ACCEPTED), "verified")
        for state in (State.PENDING, State.CONFLICTED, State.REJECTED, State.SUPERSEDED):
            self.assertNotIn(read(state), ("verified", "sourced"), state)

    def test_a_superseded_row_re_accepted_with_the_same_value_reads_as_the_accepted_row(self):
        _run()
        old = _rows(field_key=FieldKey.PUBLIC_STATUS, state=State.ACCEPTED).get()
        CompanyFieldEvidence.objects.filter(pk=old.pk).update(state=State.SUPERSEDED)
        new = CompanyFieldEvidence.objects.create(
            organization=_org(), field_key=old.field_key, state=State.ACCEPTED,
            value_text=old.value_text, source_url=old.source_url, source_domain=old.source_domain,
            observation=old.observation, observed_at=old.observed_at,
            validation_version="company-profile.v3", extractor_version="company-profile.v3",
            last_checked_at=old.last_checked_at, last_verified_at=old.last_verified_at,
        )
        self.assertEqual(_ordinary_match().evidence_ids.count(old.pk), 1)
        pairs = _annotated_requirements(get_user_model().objects.get(username=ORDINARY_USERNAME))
        confirmed = _confirmed(pairs)
        self.assertTrue(confirmed)
        _, accepted = company_evidence._evidence_rows_and_statuses([old.pk], None)
        self.assertEqual(accepted[old.pk].pk, new.pk)
        # The invariant helper treats the re-accepted row as the backing row.
        AcceptanceFixtureEvidenceTests()._assert_confirmed_requirements_are_backed(
            get_user_model().objects.get(username=ORDINARY_USERNAME)
        )


@override_settings(CACHES=LOCMEM)
class StagingUserSeesTheMatchTests(TestCase):
    """AC 18: what ``staging_user`` sees through the product after a seed."""

    def setUp(self):
        _run()
        self.user = get_user_model().objects.get(username=ORDINARY_USERNAME)

    def _signed_in_client(self):
        self.assertTrue(self.client.login(username=ORDINARY_USERNAME, password=DEFAULT_ORDINARY_PASSWORD))
        return self.client

    def test_r7_the_list_and_the_detail_serve_the_ordinary_match(self):
        match = _ordinary_match()
        for flag in (False, True):
            with self.subTest(match_results_read_enabled=flag), override_settings(
                MATCH_RESULTS_READ_ENABLED=flag
            ):
                client = self._signed_in_client()
                listing = client.get("/api/job-matches/").json()
                self.assertIn(match.pk, [row["id"] for row in listing["results"]])
                detail = client.get(f"/api/job-matches/{match.pk}/")
                self.assertEqual(detail.status_code, 200)
                paths = [r["path"] for r in detail.json()["requirements"]]
                self.assertEqual(paths, [r["path"] for r in match.requirements])

    def test_r8_one_real_refresh_publishes_the_stored_requirements(self):
        stored = _ordinary_match()
        outcome = recompute_user(self.user, reason="test", force=True)
        self.assertEqual(outcome.status, RecomputeStatus.PUBLISHED)
        published = JobMatch.objects.filter(
            user=self.user, listing=stored.listing
        ).exclude(ranker_version="staging-baseline").get()
        self.assertEqual(published.requirements, stored.requirements)
        cited = sorted(
            r["source_id"] for r in stored.requirements if r["source_kind"] == "evidence"
        )
        self.assertEqual(cited, stored.evidence_ids)
        # The persisted match stores every accepted row of the organization;
        # the command stores only the cited ones. Both accepted rows are cited
        # here, so the test requires the cited rows to be a subset of what a
        # refresh stores and that subset to be all the accepted rows.
        self.assertTrue(set(cited) <= set(published.evidence_ids))
        self.assertEqual(
            set(published.evidence_ids),
            set(_rows(state=State.ACCEPTED).values_list("pk", flat=True)),
        )

    def test_r10_the_preference_is_created_equal_to_the_evaluated_document(self):
        preference = UserPreference.objects.get(user=self.user)
        self.assertEqual(preference.preferences, fixture_preferences())
        self.assertEqual(preference.schema_version, SCHEMA_VERSION)

    def test_r10_an_existing_document_is_kept_and_reported(self):
        saved = default_preferences()
        saved["compensation"]["require_public_company"] = False
        UserPreference.objects.filter(user=self.user).update(preferences=saved)
        lines = _run()
        self.assertEqual(UserPreference.objects.get(user=self.user).preferences, saved)
        self.assertEqual(sum("saved priorities" in line.lower() for line in lines), 1)
        # The stored match is still evaluated against the fixture's document.
        self.assertEqual(_ordinary_match().requirements[0]["path"], "compensation.require_public_company")

    def test_r10_a_user_with_no_saved_preference_gets_one_on_a_later_run(self):
        UserPreference.objects.filter(user=self.user).delete()
        _run()
        self.assertEqual(
            UserPreference.objects.get(user=self.user).preferences, fixture_preferences()
        )


class StagingListingLinkTests(TestCase):
    def _listing(self):
        return JobListing.all_objects.get(external_id="staging-active-1")

    def test_r9_a_fresh_seed_links_the_listing(self):
        _run()
        self.assertEqual(self._listing().organization, _org())

    def test_r9_a_listing_without_an_organization_is_linked(self):
        _run()
        JobListing.all_objects.filter(pk=self._listing().pk).update(organization=None)
        _run()
        self.assertEqual(self._listing().organization, _org())

    def test_r9_a_listing_linked_elsewhere_is_left_alone(self):
        _run()
        other = Organization.objects.get(name="Staging Rating Source")
        JobListing.all_objects.filter(pk=self._listing().pk).update(organization=other)
        _run()
        self.assertEqual(self._listing().organization, other)


@override_settings(CACHES=LOCMEM)
class StagingAccountTests(TestCase):
    def _columns(self, username):
        user = get_user_model().objects.get(username=username)
        return {f.attname: getattr(user, f.attname) for f in user._meta.concrete_fields}

    def test_a_second_run_changes_no_column_of_either_account(self):
        _run()
        client = self.client
        self.assertTrue(client.login(username=ORDINARY_USERNAME, password=DEFAULT_ORDINARY_PASSWORD))
        before = {name: self._columns(name) for name in (ORDINARY_USERNAME, "staging_staff")}
        _run()
        self.assertEqual(
            {name: self._columns(name) for name in (ORDINARY_USERNAME, "staging_staff")}, before
        )
        self.assertEqual(client.get("/api/job-matches/").status_code, 200)

    def test_password_flag_sets_it_and_a_later_run_without_it_keeps_it(self):
        _run()
        _run(password="custom-throwaway")
        user = get_user_model().objects.get(username=ORDINARY_USERNAME)
        self.assertTrue(user.check_password("custom-throwaway"))
        _run()
        user.refresh_from_db()
        self.assertTrue(user.check_password("custom-throwaway"))

    def test_staff_account_keeps_an_operator_password_and_never_a_repository_known_one(self):
        _run(staff_password="operator-chosen-value")
        staff = get_user_model().objects.get(username="staging_staff")
        self.assertTrue(staff.check_password("operator-chosen-value"))
        _run()
        staff.refresh_from_db()
        self.assertTrue(staff.check_password("operator-chosen-value"))
        staff.set_password(DEFAULT_ORDINARY_PASSWORD)
        staff.save()
        _run()
        staff.refresh_from_db()
        self.assertFalse(staff.has_usable_password())


class StagingOutputTests(TestCase):
    def test_the_output_says_the_ordinary_match_was_rewritten(self):
        _run()
        JobMatch.objects.update(requirements=[], evidence_ids=[])
        lines = _run()
        self.assertTrue(any(line.startswith("Rewrote the stored requirements") for line in lines))


@override_settings(CACHES=LOCMEM)
class StagingResetAfterARefreshTests(TestCase):
    """The documented reset leaves nothing the product serves citing a deleted row."""

    def setUp(self):
        _run()
        self.user = get_user_model().objects.get(username=ORDINARY_USERNAME)

    def _served(self):
        """Every requirement the list and detail endpoints serve to ``staging_user``."""
        self.assertTrue(
            self.client.login(username=ORDINARY_USERNAME, password=DEFAULT_ORDINARY_PASSWORD)
        )
        served = []
        results = self.client.get("/api/job-matches/").json()["results"]
        self.assertTrue(results)
        for row in results:
            detail = self.client.get(f"/api/job-matches/{row['id']}/")
            self.assertEqual(detail.status_code, 200)
            served.append((row["id"], detail.json()["requirements"]))
        return served

    def _cited_rows_exist_and_read(self):
        existing = set(CompanyFieldEvidence.objects.values_list("pk", flat=True))
        reads = {}
        for match_id, requirements in self._served():
            for requirement in requirements:
                if requirement.get("source_kind") != "evidence":
                    continue
                self.assertIn(requirement["source_id"], existing, (match_id, requirement))
                reads.setdefault(match_id, {})[requirement["path"]] = requirement[
                    "evidence_status"
                ]["state"]
        return reads

    def test_one_refresh_the_reset_and_a_read_through_the_product(self):
        self.assertEqual(
            recompute_user(self.user, reason="test", force=True).status, RecomputeStatus.PUBLISHED
        )
        published = JobMatch.objects.filter(user=self.user).exclude(
            ranker_version="staging-baseline"
        ).get()
        old = set(published.evidence_ids)
        self.assertTrue(old)
        for flag in (False, True):
            with self.subTest(match_results_read_enabled=flag), override_settings(
                MATCH_RESULTS_READ_ENABLED=flag
            ):
                # The documented reset: delete the example company's evidence rows.
                _rows().delete()
                lines = _run()
                self.assertTrue(any(line.startswith("Republished the matches") for line in lines))
                published.refresh_from_db()
                self.assertTrue(set(published.evidence_ids))
                self.assertFalse(set(published.evidence_ids) & old)
                reads = self._cited_rows_exist_and_read()
                self.assertEqual(
                    reads[published.pk],
                    {
                        "compensation.require_public_company": "verified",
                        "funding_stage": "stale",
                    },
                )
                old = set(published.evidence_ids)
        # The reset and a run that finds nothing dangling republish nothing.
        self.assertFalse(any(line.startswith("Republished") for line in _run()))

    def test_a_refresh_that_does_not_publish_is_said_not_hidden(self):
        recompute_user(self.user, reason="test", force=True)
        _rows().delete()
        failed = mock.Mock(status=RecomputeStatus.FAILED)
        with mock.patch(
            "crank.management.commands.seed_staging_baseline.recompute_user",
            return_value=failed,
        ):
            lines = _run()
        self.assertTrue(any("refresh answered failed" in line for line in lines))

    def test_a_row_no_refresh_can_rewrite_does_not_start_one_on_every_run(self):
        recompute_user(self.user, reason="test", force=True)
        # The participant excludes the company: the next refresh publishes no
        # match for the listing and leaves the earlier row as it was.
        apply_patch_to_user(
            self.user,
            {"set": {"exclusions.companies": [TARGET_ORG_NAME]}},
            expected_revision=0,
        )
        self.assertEqual(
            recompute_user(self.user, reason="test", force=True).status,
            RecomputeStatus.PUBLISHED,
        )
        self.assertEqual(JobMatch.objects.filter(user=self.user).exclude(
            ranker_version="staging-baseline"
        ).count(), 1)
        _rows().delete()
        lines = _run()
        self.assertFalse(any(line.startswith("Republished") for line in lines), lines)
        self.assertTrue(any("earlier match row(s)" in line for line in lines), lines)
        generation = MatchResultState.objects.get(user=self.user).current_generation
        for _ in range(2):
            with mock.patch(
                "crank.management.commands.seed_staging_baseline.recompute_user"
            ) as refresh:
                lines = _run()
            refresh.assert_not_called()
            self.assertFalse(any(line.startswith("Republished") for line in lines), lines)
        self.assertEqual(
            MatchResultState.objects.get(user=self.user).current_generation, generation
        )

    def test_a_row_of_an_older_ranker_version_is_said_and_not_refreshed_again(self):
        recompute_user(self.user, reason="test", force=True)
        published = JobMatch.objects.filter(user=self.user).exclude(
            ranker_version="staging-baseline"
        ).get()
        # A row published under an older ranker version stays beside the
        # present one and is never rewritten by a refresh.
        published.pk = None
        published.ranker_version = "0.9.0"
        published.result_generation = 0
        published.save()
        _rows().delete()
        self.assertTrue(any(line.startswith("Republished") for line in _run()))
        for _ in range(2):
            with mock.patch(
                "crank.management.commands.seed_staging_baseline.recompute_user"
            ) as refresh:
                lines = _run()
            refresh.assert_not_called()
            self.assertTrue(any("earlier match row(s)" in line for line in lines), lines)

    def test_a_match_of_another_account_is_not_staging_user_s_state(self):
        other = get_user_model().objects.create_user("second_account", password="x")
        document = fixture_preferences()
        UserPreference.objects.create(
            user=other,
            preferences=document,
            preferences_markdown=to_markdown(document),
            schema_version=SCHEMA_VERSION,
            revision=7,
        )
        self.assertEqual(recompute_user(other, reason="test").status, RecomputeStatus.PUBLISHED)
        # staging_user excludes the company and publishes no match: generation
        # 1 holds no row of its own.
        apply_patch_to_user(
            self.user,
            {"set": {"exclusions.companies": [TARGET_ORG_NAME]}},
            expected_revision=0,
        )
        self.assertEqual(
            recompute_user(self.user, reason="test", force=True).status,
            RecomputeStatus.PUBLISHED,
        )
        state = MatchResultState.objects.get(user=self.user)
        self.assertFalse(JobMatch.objects.filter(user=self.user).exclude(
            ranker_version="staging-baseline"
        ).exists())
        self.assertEqual(MatchResultState.objects.get(user=other).current_generation,
                         state.current_generation)
        self.assertTrue(JobMatch.objects.filter(user=other).exists())
        _rows().delete()
        with mock.patch(
            "crank.management.commands.seed_staging_baseline.recompute_user"
        ) as refresh:
            lines = _run()
        refresh.assert_not_called()
        self.assertFalse(any("earlier match row(s)" in line for line in lines), lines)
        # The recreated priorities follow staging_user's own history, not the
        # other account's published revision (7).
        UserPreference.objects.filter(user=self.user).delete()
        _run()
        self.assertEqual(UserPreference.objects.get(user=self.user).revision, 2)

    def test_without_a_published_match_the_command_runs_no_refresh(self):
        _rows().delete()
        with mock.patch(
            "crank.management.commands.seed_staging_baseline.recompute_user"
        ) as refresh:
            _run()
        refresh.assert_not_called()


@override_settings(CACHES=LOCMEM)
class StagingRecreatedPreferenceTests(TestCase):
    def test_a_recreated_row_starts_above_the_published_revision(self):
        _run()
        user = get_user_model().objects.get(username=ORDINARY_USERNAME)
        # The participant changed priorities twice and one refresh published
        # results for revision 2.
        UserPreference.objects.filter(user=user).update(revision=2)
        self.assertEqual(
            recompute_user(user, reason="test", force=True).status, RecomputeStatus.PUBLISHED
        )
        UserPreference.objects.filter(user=user).delete()
        _run()
        preference = UserPreference.objects.get(user=user)
        self.assertEqual(preference.preferences, fixture_preferences())
        self.assertEqual(preference.revision, 3)
        self.assertEqual(
            recompute_user(user, reason="test", force=True).status, RecomputeStatus.PUBLISHED
        )

    def test_a_row_recreated_before_any_refresh_starts_at_revision_0(self):
        _run()
        user = get_user_model().objects.get(username=ORDINARY_USERNAME)
        UserPreference.objects.filter(user=user).delete()
        _run()
        self.assertEqual(UserPreference.objects.get(user=user).revision, 0)

    def test_an_undo_from_before_the_reset_cannot_overwrite_the_recreated_priorities(self):
        _run()
        user = get_user_model().objects.get(username=ORDINARY_USERNAME)
        apply_patch_to_user(
            user, {"set": {"exclusions.companies": ["Acme One"]}}, expected_revision=0
        )
        self.assertEqual(
            recompute_user(user, reason="test", force=True).status, RecomputeStatus.PUBLISHED
        )
        # Two more saves after the last publish (recompute is off by default).
        second = apply_patch_to_user(
            user, {"set": {"exclusions.companies": ["Acme Two"]}}, expected_revision=1
        )
        apply_patch_to_user(
            user, {"set": {"exclusions.companies": ["Acme Three"]}}, expected_revision=2
        )
        UserPreference.objects.filter(user=user).delete()
        _run()
        self.assertGreater(UserPreference.objects.get(user=user).revision, 3)
        with self.assertRaises(StalePreferenceError):
            undo_preference_change(user, second["undo"])
        self.assertEqual(UserPreference.objects.get(user=user).preferences, fixture_preferences())

    def test_a_delete_through_the_product_leaves_the_same_protection(self):
        from crank.services.preferences import delete_user_preference

        _run()
        user = get_user_model().objects.get(username=ORDINARY_USERNAME)
        apply_patch_to_user(
            user, {"set": {"exclusions.companies": ["Acme One"]}}, expected_revision=0
        )
        self.assertEqual(
            recompute_user(user, reason="test", force=True).status, RecomputeStatus.PUBLISHED
        )
        second = apply_patch_to_user(
            user, {"set": {"exclusions.companies": ["Acme Two"]}}, expected_revision=1
        )
        apply_patch_to_user(
            user, {"set": {"exclusions.companies": ["Acme Three"]}}, expected_revision=2
        )
        delete_user_preference(user)
        _run()
        self.assertGreaterEqual(UserPreference.objects.get(user=user).revision, 4)
        with self.assertRaises(StalePreferenceError):
            undo_preference_change(user, second["undo"])
        self.assertEqual(UserPreference.objects.get(user=user).preferences, fixture_preferences())
