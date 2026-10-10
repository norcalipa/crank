# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Read-time evidence status for stored match requirements (issue #473)."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from crank.models.company_profile import CompanyFieldEvidence
from crank.models.organization import Organization
from crank.services.company_evidence import (
    FIELD_FRESHNESS_POLICY,
    _evidence_rows_and_statuses,
    _strictly_readable,
    annotate_requirement_evidence,
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

    def _statuses(self, ids, *, now=None, **overrides):
        """Each id's status as the one public path gives it: per requirement.

        Every id is cited by a work-mode requirement that matched "remote",
        which is what the default row states.
        """
        requirements = [
            {"path": "work_location.modes", "status": "match", "observed": "remote",
             "source_kind": "evidence", "source_id": pk, "scope_ok": True, **overrides}
            for pk in ids
        ]
        annotated = annotate_requirement_evidence([requirements], now=now)[0]
        return {r["source_id"]: r["evidence_status"] for r in annotated}

    def test_verified_stale_superseded_and_missing_in_one_query(self):
        verified = self._row()
        stale = self._row(age_days=FIELD_FRESHNESS_POLICY[FieldKey.RTO_POLICY] + 1)
        never = self._row(age_days=None)
        superseded = self._row(state=State.SUPERSEDED)
        rejected = self._row(state=State.REJECTED)
        missing_id = rejected.pk + 1000

        with self.assertNumQueries(1):
            statuses = self._statuses(
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
        statuses = self._statuses([boundary.pk, past.pk], now=self.now)
        self.assertEqual(statuses[boundary.pk]["state"], "verified")
        self.assertEqual(statuses[past.pk]["state"], "stale")

    def test_no_ids_runs_no_query_and_ignores_non_integer_ids(self):
        gone = {"state": "missing", "last_verified_at": None, "source_domain": None}
        with self.assertNumQueries(0):
            self.assertEqual(self._statuses([]), {})
            # Not an id: nothing is looked up, and nothing reads as verified.
            self.assertEqual(
                self._statuses([None, "7", True, 1.5]),
                {None: gone, "7": gone, True: gone, 1.5: gone},
            )
            self.assertEqual(_evidence_rows_and_statuses([None, "7", True, 1.5], None), ({}, {}))

    def test_prose_and_fields_without_a_strict_reader_are_sourced_not_verified(self):
        prose = self._row(value_text="Remote first, with five office days a quarter")
        locations = self._row(FieldKey.LOCATIONS, value_text="Berlin, Germany")
        blank_domain = self._row(value_text="hybrid", source_domain="")
        statuses = self._statuses([prose.pk, locations.pk, blank_domain.pk])
        self.assertEqual(statuses[prose.pk]["state"], "sourced")
        self.assertEqual(statuses[locations.pk]["state"], "sourced")
        # "hybrid" is a whole-value reading, but not of an outcome that read
        # "remote": the value alone never earns the mark.
        self.assertEqual(statuses[blank_domain.pk]["state"], "sourced")
        stated = self._statuses([blank_domain.pk], observed="hybrid")[blank_domain.pk]
        self.assertEqual(stated["state"], "verified")
        self.assertIsNone(stated["source_domain"])

    def test_a_fact_alone_is_never_verified_only_a_requirement_is(self):
        """No helper answers "verified" for a row by its value (round-1 MAJOR 1)."""
        from crank.services import company_evidence

        self.assertFalse(hasattr(company_evidence, "evidence_status_for_ids"))
        hybrid = self._row(value_text="Hybrid")
        remote = self._row(value_text="Remote")
        stale = self._row(value_text="Remote", age_days=4000)
        statuses, rows = _evidence_rows_and_statuses(
            [hybrid.pk, remote.pk, stale.pk], self.now
        )
        self.assertEqual(
            {pk: status["state"] for pk, status in statuses.items()},
            {hybrid.pk: "sourced", remote.pk: "sourced", stale.pk: "stale"},
        )
        self.assertEqual(set(rows), {hybrid.pk, remote.pk, stale.pk})
        # The same rows through a requirement: only the stated outcome is verified.
        by_requirement = self._statuses([hybrid.pk, remote.pk, stale.pk], now=self.now)
        self.assertEqual(
            {pk: status["state"] for pk, status in by_requirement.items()},
            {hybrid.pk: "sourced", remote.pk: "verified", stale.pk: "stale"},
        )

    def test_stale_wins_over_an_unreadable_value(self):
        row = self._row(value_text="It depends on the team", age_days=400)
        self.assertEqual(self._statuses([row.pk])[row.pk]["state"], "stale")

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


#: Whole-value forms an accepted row may be called "Verified" for, per
#: requirement, written out by hand (not derived from the service's tables).
_STATED_FORMS = {
    "work_location.modes": {
        "r", "remote", "h", "hybrid", "o", "in-office", "in office", "onsite",
    },
    # Only a remote policy states a day count; 3 and 5 are matching's guesses.
    "work_location.max_in_office_days": {"r", "remote"},
    "compensation.require_public_company": {
        "public", "public company", "private", "private company",
    },
    "funding_stage": {
        "s", "a", "b", "c", "d", "e", "f", "x", "o", "p",
        "seed", "series a", "series b", "series c", "series d", "series e",
        "series f", "series g or later", "other private", "public",
    },
    "vesting.prefer_accelerated": {"true", "yes", "1", "false", "no", "0"},
    "work_location.countries": set(),
}

_VALUE_CORPUS = [
    "Remote", "R", "remote", "Hybrid", "H", "hybrid", "In-Office", "in office",
    "Onsite", "O", "Remote first", "Hybrid 3 days", "Hybrid, two days a week",
    "Five days in office, no exceptions", "It depends on the team",
    "Public", "Public company", "Private", "private company", "Publicly listed on NASDAQ",
    "Not public yet", "Seed", "Pre-seed", "pre seed", "Series A", "series b", "Series G",
    "Series G or Later", "Series X", "Other Private", "X", "P", "S",
    "raised a Series B last year", "yes", "Yes", "no", "true", "1", "0", "sometimes",
    "Berlin, Germany", "United States", "Germany",
]

_CRITERIA_VARIANTS = [
    {"work_modes": frozenset({"remote"}), "max_in_office_days": 0},
    {"work_modes": frozenset({"hybrid"}), "max_in_office_days": 2},
    {"work_modes": frozenset({"in-office", "hybrid"}), "max_in_office_days": 3},
    {"work_modes": frozenset({"remote"}), "max_in_office_days": 5},
]


class RequirementStatedByEvidenceTests(TestCase):
    """"Verified" is decided per requirement, from what the evidence states."""

    def setUp(self):
        from crank.agents.jobs import matching

        self.matching = matching
        self.now = timezone.now()
        self.organization = Organization.objects.create(name="Example Labs")
        self.listing = type(
            "Listing", (), {"location_text": "Berlin, Germany", "title": "Sales Engineer",
                            "is_remote": None, "organization": self.organization,
                            "source_metadata": {}},
        )()

    def _row(self, field_key, value_text, **kwargs):
        values = {
            "organization": self.organization,
            "field_key": field_key,
            "value_text": value_text,
            "source_url": "https://example.test/about",
            "source_domain": "example.test",
            "observed_at": self.now,
            "last_verified_at": self.now,
            "validation_version": "v1",
            "extractor_version": "v1",
            "state": State.ACCEPTED,
        }
        values.update(kwargs)
        return CompanyFieldEvidence.objects.create(**values)

    def _criteria(self, **overrides):
        values = {
            "require_public_company": True,
            "countries": frozenset({"germany"}),
            "funding_stages": frozenset({"series b", "seed"}),
            "prefer_accelerated": True,
            "work_modes": frozenset({"remote"}),
            "max_in_office_days": 3,
        }
        values.update(overrides)
        return self.matching.JobCriteria(**values)

    def _evaluate(self, path, row, criteria=None):
        """Matching's own outcome for ``path`` from ``row``, then annotated."""
        outcome = self.matching._eval_requirement(
            path, self.listing, self.organization, criteria or self._criteria(),
            {row.field_key: row},
        )
        return annotate_requirement_evidence([[outcome.as_dict()]], now=self.now)[0][0]

    def _state(self, path, field_key, value_text, criteria=None, **row_kwargs):
        requirement = self._evaluate(path, self._row(field_key, value_text, **row_kwargs), criteria)
        return requirement["status"], requirement["observed"], requirement["evidence_status"]["state"]

    def test_hybrid_does_not_verify_an_in_office_day_count(self):
        # The reviewer's case: the evidence says "Hybrid"; the 3 is a guess.
        path = "work_location.max_in_office_days"
        self.assertEqual(
            self._state(path, FieldKey.RTO_POLICY, "Hybrid"), ("match", 3, "sourced")
        )
        self.assertEqual(
            self._state(path, FieldKey.RTO_POLICY, "Hybrid",
                        self._criteria(max_in_office_days=2)),
            ("mismatch", 3, "sourced"),
        )
        self.assertEqual(
            self._state(path, FieldKey.RTO_POLICY, "In-Office"), ("mismatch", 5, "sourced")
        )
        # The same fact does state the work mode.
        self.assertEqual(
            self._state("work_location.modes", FieldKey.RTO_POLICY, "Hybrid"),
            ("mismatch", "hybrid", "verified"),
        )

    def test_remote_states_zero_office_days(self):
        self.assertEqual(
            self._state("work_location.max_in_office_days", FieldKey.RTO_POLICY, "Remote",
                        self._criteria(max_in_office_days=0)),
            ("match", 0, "verified"),
        )

    def test_each_requirement_kind(self):
        cases = [
            ("work_location.modes", FieldKey.RTO_POLICY, "Remote", ("match", "remote", "verified")),
            ("work_location.modes", FieldKey.RTO_POLICY, "Remote first",
             ("match", "remote", "sourced")),
            ("compensation.require_public_company", FieldKey.PUBLIC_STATUS, "Public company",
             ("match", "Public company", "verified")),
            ("compensation.require_public_company", FieldKey.PUBLIC_STATUS, "private",
             ("mismatch", "private", "verified")),
            ("compensation.require_public_company", FieldKey.PUBLIC_STATUS,
             "Publicly listed on NASDAQ", ("match", "Publicly listed on NASDAQ", "sourced")),
            ("funding_stage", FieldKey.FUNDING_ROUND, "Series B", ("match", "b", "verified")),
            ("funding_stage", FieldKey.FUNDING_ROUND, "S", ("match", "s", "verified")),
            ("funding_stage", FieldKey.FUNDING_ROUND, "Series C", ("mismatch", "c", "verified")),
            # An alias only matching's canonicaliser resolves is not stated.
            ("funding_stage", FieldKey.FUNDING_ROUND, "Pre-seed", ("match", "s", "sourced")),
            ("funding_stage", FieldKey.FUNDING_ROUND, "Series G", ("mismatch", "x", "sourced")),
            ("vesting.prefer_accelerated", FieldKey.ACCELERATED_VESTING, "yes",
             ("match", True, "verified")),
            ("vesting.prefer_accelerated", FieldKey.ACCELERATED_VESTING, "no",
             ("mismatch", False, "verified")),
            # Matching reads anything but true/yes/1 as "no": a guess.
            ("vesting.prefer_accelerated", FieldKey.ACCELERATED_VESTING, "sometimes",
             ("mismatch", False, "sourced")),
            # A country found by substring in a locations value is never stated.
            ("work_location.countries", FieldKey.LOCATIONS, "Germany",
             ("match", "germany", "sourced")),
        ]
        for path, field_key, value_text, expected in cases:
            with self.subTest(path=path, value_text=value_text):
                self.assertEqual(self._state(path, field_key, value_text), expected)

    def test_scoped_row_is_never_verified_even_when_the_listing_is_in_scope(self):
        for scope in ({"countries": ["Germany"]}, {"role_families": ["Sales"]}):
            with self.subTest(scope=scope):
                row = self._row(FieldKey.RTO_POLICY, "Remote", scope_json=scope)
                requirement = self._evaluate("work_location.modes", row)
                self.assertEqual(
                    (requirement["status"], requirement["scope_ok"]), ("match", True)
                )
                self.assertEqual(requirement["evidence_status"]["state"], "sourced")
                self.assertEqual(
                    _evidence_rows_and_statuses([row.pk], self.now)[0][row.pk]["state"],
                    "sourced",
                )
        # Attribution is not scope.
        attributed = self._row(
            FieldKey.RTO_POLICY, "Remote", scope_json={"claimed_domain": "example.test"}
        )
        self.assertEqual(
            self._evaluate("work_location.modes", attributed)["evidence_status"]["state"],
            "verified",
        )

    def test_outcome_that_disagrees_with_the_stated_value_is_not_verified(self):
        row = self._row(FieldKey.RTO_POLICY, "Hybrid")
        funding = self._row(FieldKey.FUNDING_ROUND, "Series B")
        public = self._row(FieldKey.PUBLIC_STATUS, "Private")
        vesting = self._row(FieldKey.ACCELERATED_VESTING, "yes")

        def state(path, pk, **overrides):
            requirement = {"path": path, "status": "match", "source_kind": "evidence",
                           "source_id": pk, "scope_ok": True, **overrides}
            return annotate_requirement_evidence([[requirement]], now=self.now)[0][0][
                "evidence_status"]["state"]

        self.assertEqual(state("work_location.modes", row.pk, observed="hybrid"), "verified")
        self.assertEqual(state("work_location.modes", row.pk, observed="remote"), "sourced")
        self.assertEqual(state("work_location.max_in_office_days", row.pk, observed=0), "sourced")
        self.assertEqual(state("funding_stage", funding.pk, observed="b"), "verified")
        self.assertEqual(state("funding_stage", funding.pk, observed="c"), "sourced")
        self.assertEqual(state("funding_stage", funding.pk, observed=None), "sourced")
        self.assertEqual(
            state("compensation.require_public_company", public.pk, status="mismatch"),
            "verified",
        )
        self.assertEqual(state("compensation.require_public_company", public.pk), "sourced")
        self.assertEqual(state("vesting.prefer_accelerated", vesting.pk, observed=True), "verified")
        self.assertEqual(state("vesting.prefer_accelerated", vesting.pk, observed=1), "sourced")
        # A requirement the field is not listed for, or a missing scope flag.
        self.assertEqual(state("funding_stage", row.pk, observed="h"), "sourced")
        self.assertEqual(state("culture", row.pk, observed="hybrid"), "sourced")
        self.assertEqual(
            state("work_location.modes", row.pk, observed="hybrid", scope_ok=None), "sourced"
        )

    def test_stated_readers_refuse_values_they_do_not_list(self):
        """Each reader stands on its own, whatever the row-level check let through."""
        from crank.services import company_evidence as service

        match = {"status": "match", "observed": None}
        self.assertFalse(service._states_public_status("Publicly listed", match))
        self.assertFalse(service._states_public_status("", {"status": None}))
        self.assertTrue(service._states_public_status("PUBLIC_COMPANY", match))
        self.assertFalse(service._states_work_mode("mostly remote", {**match, "observed": None}))
        self.assertFalse(service._states_office_days("Remote", {**match, "observed": False}))
        self.assertFalse(service._states_funding_stage("Series Q", {**match, "observed": "series q"}))
        self.assertFalse(service._states_accelerated_vesting("sometimes", match))

    def test_a_listed_reader_cannot_verify_a_value_with_no_strict_reading(self):
        """The row-level check stands on its own too, whatever a reader lets through.

        Every reader listed today refuses what ``_strictly_readable`` refuses,
        so only a reader that accepts anything shows the check deciding: a
        pair added to the allowlist cannot verify prose, or a field with no
        strict reading at all (round-2 NIT 5).
        """
        from crank.services import company_evidence as service

        strict = self._row(FieldKey.RTO_POLICY, "Hybrid")
        prose = self._row(FieldKey.RTO_POLICY, "Remote first, with five office days a quarter")
        locations = self._row(FieldKey.LOCATIONS, "Berlin, Germany")

        def state(path, pk, observed):
            requirement = {"path": path, "status": "match", "observed": observed,
                           "source_kind": "evidence", "source_id": pk, "scope_ok": True}
            return annotate_requirement_evidence([[requirement]], now=self.now)[0][0][
                "evidence_status"]["state"]

        accepts_anything = mock.Mock(return_value=True)
        with mock.patch.dict(service._REQUIREMENT_STATED_BY, {
            ("work_location.modes", FieldKey.RTO_POLICY): accepts_anything,
            ("work_location.countries", FieldKey.LOCATIONS): accepts_anything,
        }):
            # The patched reader is the one consulted: it verifies an outcome
            # the real reader refuses ("Hybrid" does not state "remote").
            self.assertEqual(state("work_location.modes", strict.pk, "remote"), "verified")
            accepts_anything.assert_called_once()
            self.assertEqual(state("work_location.modes", prose.pk, "remote"), "sourced")
            self.assertEqual(state("work_location.countries", locations.pk, "germany"), "sourced")
            # ...and it was never asked about either value.
            accepts_anything.assert_called_once()
        self.assertEqual(state("work_location.modes", strict.pk, "remote"), "sourced")

    def test_only_a_stated_whole_value_can_be_verified(self):
        """Property: across every evidence-backed requirement, value and
        criteria, matching's real outcome is Verified only for a hand-listed
        stated form, and never when it rests on an inferred day count."""
        paths = self.matching._EVIDENCE_FIELD_KEY
        self.assertEqual(set(paths), set(_STATED_FORMS))
        verified = set()
        for path, field_key in paths.items():
            for value_text in _VALUE_CORPUS:
                row = self._row(field_key, value_text)
                for variant in _CRITERIA_VARIANTS:
                    requirement = self._evaluate(path, row, self._criteria(**variant))
                    state = requirement["evidence_status"]["state"]
                    self.assertIn(state, ("verified", "sourced"))
                    if state != "verified":
                        continue
                    verified.add(path)
                    with self.subTest(path=path, value_text=value_text, variant=variant):
                        self.assertIn(" ".join(value_text.casefold().split()), _STATED_FORMS[path])
                        self.assertIn(requirement["status"], ("match", "mismatch"))
                        if path == "work_location.max_in_office_days":
                            # 3 and 5 exist only in matching's reader.
                            self.assertEqual(requirement["observed"], 0)
                            self.assertNotIn(self.matching._rto_days(value_text), (3, 5))
        # The property is not vacuous: every listed kind does get verified.
        self.assertEqual(verified, {p for p, forms in _STATED_FORMS.items() if forms})

    def test_a_new_evidence_backed_requirement_defaults_to_not_verified(self):
        from crank.services import company_evidence

        listed = {path for path, _ in company_evidence._REQUIREMENT_STATED_BY}
        self.assertEqual(
            listed, {p for p, forms in _STATED_FORMS.items() if forms}
        )
        row = self._row(FieldKey.RTO_POLICY, "Remote")
        requirement = {"path": "some.future_requirement", "status": "match",
                       "observed": "remote", "source_kind": "evidence",
                       "source_id": row.pk, "scope_ok": True}
        self.assertEqual(
            annotate_requirement_evidence([[requirement]], now=self.now)[0][0][
                "evidence_status"]["state"],
            "sourced",
        )
