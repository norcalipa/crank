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

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from crank.management.commands.seed_staging_baseline import (
    EVIDENCE_VERSION,
    ORDINARY_USERNAME,
    TARGET_ORG_NAME,
)
from crank.agents.jobs.matching import project_criteria
from crank.models.company_profile import CompanyFieldEvidence
from crank.models.job_match import JobMatch
from crank.models.organization import Organization
from crank.models.preference import SCHEMA_VERSION, default_preferences
from crank.services import company_evidence
from crank.services.company_evidence import resolve_field_evidence_for_orgs
from crank.services.job_matching import evaluate_org_requirements

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
        for organization_id, requirements in zip(organization_ids, annotated)
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
            row = CompanyFieldEvidence.objects.filter(pk=requirement["source_id"]).first()
            self.assertIsNotNone(row, f"no evidence row for confirmed {requirement}")
            self.assertEqual(row.state, State.ACCEPTED, requirement)
            self.assertEqual(row.organization_id, organization_id, requirement)

    def test_staging_baseline_confirmed_requirements_have_accepted_evidence(self):
        _seed("seed_staging_baseline")
        user = get_user_model().objects.get(username=ORDINARY_USERNAME)
        self._assert_confirmed_requirements_are_backed(user)

    def test_seed_e2e_confirmed_requirements_have_accepted_evidence(self):
        from crank.management.commands.seed_e2e import E2E_USERNAME

        _seed("seed_e2e")
        user = get_user_model().objects.get(username=E2E_USERNAME)
        # seed_e2e's accepted evidence is prose ("Remote-first"), long past its
        # window (beta) or off-domain, so today none of it is confirmed. The
        # invariant holds for whatever it does confirm, now or after a later
        # change to that seed; nothing pins its rows or that it stays empty.
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
        # Hybrid is stated only by a conflicted claim, which never verifies
        # anything: the requirement rests on the company's own field, and the
        # unknown field is never reported as sourced evidence.
        self.assertNotIn(statuses["work_location.modes"], ("verified", "stale", "sourced"))
        self.assertNotIn(statuses["vesting.prefer_accelerated"], ("verified", "stale", "sourced"))
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
