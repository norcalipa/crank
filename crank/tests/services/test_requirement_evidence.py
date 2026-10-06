# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Read-time evidence status for stored match requirements (issue #473)."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from crank.models.company_profile import CompanyFieldEvidence
from crank.models.organization import Organization
from crank.services.company_evidence import (
    FIELD_FRESHNESS_POLICY,
    _strictly_readable,
    annotate_requirement_evidence,
    evidence_status_for_ids,
)

FieldKey = CompanyFieldEvidence.FieldKey
State = CompanyFieldEvidence.State


class EvidenceStatusTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.organization = Organization.objects.create(
            name="Example Labs", url="https://example.test"
        )

    def _row(self, field_key=FieldKey.RTO_POLICY, *, state=State.ACCEPTED, age_days=1,
             value_text="Remote", **kwargs):
        verified_at = None if age_days is None else self.now - timedelta(days=age_days)
        values = {
            "organization": self.organization,
            "field_key": field_key,
            "value_text": value_text,
            "source_url": "https://example.test/about",
            "source_domain": "example.test",
            "observed_at": verified_at or self.now,
            "last_verified_at": verified_at,
            "validation_version": "v1",
            "extractor_version": "v1",
            "state": state,
        }
        values.update(kwargs)
        return CompanyFieldEvidence.objects.create(**values)

    def test_verified_stale_superseded_and_missing_in_one_query(self):
        verified = self._row()
        stale = self._row(age_days=FIELD_FRESHNESS_POLICY[FieldKey.RTO_POLICY] + 1)
        never = self._row(age_days=None)
        superseded = self._row(state=State.SUPERSEDED)
        rejected = self._row(state=State.REJECTED)
        missing_id = rejected.pk + 1000

        with self.assertNumQueries(1):
            statuses = evidence_status_for_ids(
                [verified.pk, stale.pk, never.pk, superseded.pk, rejected.pk, missing_id],
                now=self.now,
            )

        self.assertEqual(
            statuses[verified.pk],
            {
                "state": "verified",
                "last_verified_at": verified.last_verified_at.isoformat(),
                "source_domain": "example.test",
            },
        )
        self.assertEqual(statuses[stale.pk]["state"], "stale")
        self.assertEqual(
            statuses[stale.pk]["last_verified_at"], stale.last_verified_at.isoformat()
        )
        self.assertEqual(
            statuses[never.pk],
            {"state": "stale", "last_verified_at": None, "source_domain": "example.test"},
        )
        # A row that is no longer the accepted fact exposes nothing about itself.
        closed = {"state": "superseded", "last_verified_at": None, "source_domain": None}
        self.assertEqual(statuses[superseded.pk], closed)
        self.assertEqual(statuses[rejected.pk], closed)
        self.assertEqual(
            statuses[missing_id],
            {"state": "missing", "last_verified_at": None, "source_domain": None},
        )

    def test_policy_boundary_is_fresh_and_one_day_past_is_stale(self):
        days = FIELD_FRESHNESS_POLICY[FieldKey.RTO_POLICY]
        boundary = self._row(age_days=days)
        past = self._row(age_days=days + 1)
        statuses = evidence_status_for_ids([boundary.pk, past.pk], now=self.now)
        self.assertEqual(statuses[boundary.pk]["state"], "verified")
        self.assertEqual(statuses[past.pk]["state"], "stale")

    def test_no_ids_runs_no_query_and_ignores_non_integer_ids(self):
        with self.assertNumQueries(0):
            self.assertEqual(evidence_status_for_ids([]), {})
            self.assertEqual(evidence_status_for_ids([None, "7", True, 1.5]), {})

    def test_prose_and_fields_without_a_strict_reader_are_sourced_not_verified(self):
        prose = self._row(value_text="Remote first, with five office days a quarter")
        locations = self._row(FieldKey.LOCATIONS, value_text="Berlin, Germany")
        blank_domain = self._row(value_text="hybrid", source_domain="")
        statuses = evidence_status_for_ids([prose.pk, locations.pk, blank_domain.pk])
        self.assertEqual(statuses[prose.pk]["state"], "sourced")
        self.assertEqual(statuses[locations.pk]["state"], "sourced")
        self.assertEqual(statuses[blank_domain.pk]["state"], "verified")
        self.assertIsNone(statuses[blank_domain.pk]["source_domain"])

    def test_stale_wins_over_an_unreadable_value(self):
        row = self._row(value_text="It depends on the team", age_days=400)
        self.assertEqual(evidence_status_for_ids([row.pk])[row.pk]["state"], "stale")

    def test_strict_readings_per_field(self):
        cases = [
            (FieldKey.RTO_POLICY, "In Office", True),
            (FieldKey.RTO_POLICY, "mostly remote", False),
            (FieldKey.ACCELERATED_VESTING, "yes", True),
            (FieldKey.ACCELERATED_VESTING, "sometimes", False),
            (FieldKey.PUBLIC_STATUS, "Public  company", True),
            (FieldKey.PUBLIC_STATUS, "private", True),
            (FieldKey.PUBLIC_STATUS, "Not public yet", False),
            (FieldKey.FUNDING_ROUND, "series b", True),
            (FieldKey.FUNDING_ROUND, "X", True),
            (FieldKey.FUNDING_ROUND, "Series G or Later", True),
            (FieldKey.FUNDING_ROUND, "raised a Series B last year", False),
            (FieldKey.COMPANY_NAME, "Example Labs", False),
        ]
        for field_key, value_text, expected in cases:
            with self.subTest(field_key=field_key, value_text=value_text):
                self.assertIs(_strictly_readable(field_key, value_text), expected)


class AnnotateRequirementEvidenceTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.organization = Organization.objects.create(name="Example Labs")
        self.row = CompanyFieldEvidence.objects.create(
            organization=self.organization,
            field_key=FieldKey.RTO_POLICY,
            value_text="Remote",
            source_url="https://example.test/about",
            source_domain="example.test",
            observed_at=self.now,
            last_verified_at=self.now,
            validation_version="v1",
            extractor_version="v1",
            state=State.ACCEPTED,
        )

    def _requirement(self, **overrides):
        requirement = {
            "path": "work_location.modes",
            "status": "match",
            "observed": "remote",
            "source_kind": "evidence",
            "source_id": self.row.pk,
            "scope_ok": True,
        }
        requirement.update(overrides)
        return requirement

    def test_one_query_for_many_lists_and_stored_dicts_are_not_mutated(self):
        stored = [
            [self._requirement()],
            [
                self._requirement(path="funding_stage", source_kind="field",
                                  source_id="organization.funding_round"),
                self._requirement(path="compensation.minimum_salary", source_kind="field",
                                  source_id="listing.compensation_min"),
                self._requirement(path="industry", source_kind="field",
                                  source_id="source_metadata.industry"),
                self._requirement(path="culture", status="unknown", source_kind=None,
                                  source_id=None),
            ],
            None,
        ]
        snapshot = [[dict(r) for r in (rs or [])] for rs in stored]

        with self.assertNumQueries(1):
            annotated = annotate_requirement_evidence(stored, now=self.now)

        self.assertEqual([[dict(r) for r in (rs or [])] for rs in stored], snapshot)
        self.assertNotIn("evidence_status", stored[0][0])
        self.assertEqual(annotated[0][0]["evidence_status"]["state"], "verified")
        self.assertEqual(annotated[0][0]["evidence_status"]["source_domain"], "example.test")
        self.assertEqual(
            annotated[1][0]["evidence_status"],
            {"state": "profile", "last_verified_at": None, "source_domain": None},
        )
        # Listing data, merged metadata and unsourced outcomes carry no status.
        self.assertIsNone(annotated[1][1]["evidence_status"])
        self.assertIsNone(annotated[1][2]["evidence_status"])
        self.assertIsNone(annotated[1][3]["evidence_status"])
        self.assertEqual(annotated[2], [])
        # Every original key survives.
        self.assertEqual(
            {k: v for k, v in annotated[0][0].items() if k != "evidence_status"},
            stored[0][0],
        )

    def test_no_evidence_reference_means_no_query(self):
        with self.assertNumQueries(0):
            annotated = annotate_requirement_evidence(
                [[self._requirement(source_kind="field", source_id="organization.rto_policy")]]
            )
        self.assertEqual(annotated[0][0]["evidence_status"]["state"], "profile")

    def test_evidence_that_did_not_decide_the_outcome_is_not_verified(self):
        annotated = annotate_requirement_evidence(
            [[
                self._requirement(status="unknown", observed=None),
                self._requirement(scope_ok=False),
                self._requirement(status="mismatch"),
            ]],
            now=self.now,
        )[0]
        self.assertEqual(annotated[0]["evidence_status"]["state"], "sourced")
        self.assertEqual(annotated[1]["evidence_status"]["state"], "sourced")
        self.assertEqual(annotated[2]["evidence_status"]["state"], "verified")

    def test_superseded_deleted_and_malformed_references(self):
        gone = self.row.pk + 500
        CompanyFieldEvidence.objects.filter(pk=self.row.pk).update(state=State.SUPERSEDED)
        annotated = annotate_requirement_evidence(
            [[
                self._requirement(),
                self._requirement(source_id=gone),
                self._requirement(source_id="12"),
                self._requirement(source_id=[self.row.pk]),
                self._requirement(source_id=True),
                "not-a-dict",
            ]],
            now=self.now,
        )[0]
        self.assertEqual(annotated[0]["evidence_status"]["state"], "superseded")
        for item in annotated[1:5]:
            self.assertEqual(
                item["evidence_status"],
                {"state": "missing", "last_verified_at": None, "source_domain": None},
            )
        self.assertEqual(annotated[5], "not-a-dict")

    def test_stale_stays_stale_whatever_the_outcome(self):
        CompanyFieldEvidence.objects.filter(pk=self.row.pk).update(
            last_verified_at=self.now - timedelta(days=400)
        )
        annotated = annotate_requirement_evidence(
            [[self._requirement(), self._requirement(status="unknown")]], now=self.now
        )[0]
        self.assertEqual([a["evidence_status"]["state"] for a in annotated], ["stale", "stale"])
