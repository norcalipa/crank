# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for accepted field-level evidence (issue #460)."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
import pytest

from crank.models.company_profile import (
    CompanyFieldEvidence,
    CompanyProfileObservation,
)
from crank.models.monitoring import OperationalChangeAudit
from crank.models.organization import Organization
from crank.models.publication import PublicationEvent
from crank.services.company_evidence import (
    AUTO_APPLY_FIELDS,
    DEFAULT_FRESHNESS_DAYS,
    FIELD_FRESHNESS_POLICY,
    CorrectionNotAcceptable,
    OPEN_CLAIM_STATES,
    REVIEW_REQUIRED_FIELDS,
    EvidenceNotAcceptable,
    accept_correction,
    accept_observation_fields,
    field_evidence_payload,
    is_stale,
    record_check,
    resolve_field_evidence,
    resolve_field_evidence_for_orgs,
)

FieldKey = CompanyFieldEvidence.FieldKey
State = CompanyFieldEvidence.State
Status = CompanyProfileObservation.Status


def make_observation(organization, *, status=Status.AUTO_APPLIED, observed_at=None, **fields):
    values = {
        "organization": organization,
        "source_url": "https://jobs.example.test/about",
        "observed_domain": "example.test",
        "observed_name": "Example Labs",
        "description": "Build useful APIs.",
        "locations": ["Remote", "Berlin"],
        "rto_evidence": "Remote first",
        "funding_evidence": "Series A",
        "public_status_evidence": "Private company",
        "observed_at": observed_at or timezone.now(),
        "extraction_version": "firecrawl-company-profile.v1",
        "status": status,
        "fingerprint": f"fp-{timezone.now().timestamp()}-{status}",
    }
    values.update(fields)
    return CompanyProfileObservation.objects.create(**values)


class AcceptObservationFieldsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Example Labs", url="https://example.test"
        )

    def test_rejects_pending_observation(self):
        observation = make_observation(self.organization, status=Status.PENDING)
        with pytest.raises(EvidenceNotAcceptable):
            accept_observation_fields(observation)
        self.assertEqual(CompanyFieldEvidence.objects.count(), 0)

    def test_rejects_rejected_observation(self):
        observation = make_observation(self.organization, status=Status.REJECTED)
        with pytest.raises(EvidenceNotAcceptable):
            accept_observation_fields(observation)
        self.assertEqual(CompanyFieldEvidence.objects.count(), 0)

    def test_rejects_conflicted_observation(self):
        observation = make_observation(self.organization, status=Status.CONFLICTED)
        with pytest.raises(EvidenceNotAcceptable):
            accept_observation_fields(observation)
        self.assertEqual(CompanyFieldEvidence.objects.count(), 0)

    def test_rejects_observation_without_organization(self):
        observation = make_observation(None, status=Status.AUTO_APPLIED)
        with pytest.raises(EvidenceNotAcceptable):
            accept_observation_fields(observation)
        self.assertEqual(CompanyFieldEvidence.objects.count(), 0)

    def test_accept_from_auto_applied_creates_accepted_rows(self):
        now = timezone.now()
        observation = make_observation(self.organization, status=Status.AUTO_APPLIED)

        created = accept_observation_fields(observation, now=now)

        keys = {row.field_key for row in created}
        self.assertEqual(
            keys,
            {
                FieldKey.COMPANY_NAME,
                FieldKey.COMPANY_DOMAIN,
                FieldKey.LOCATIONS,
                FieldKey.RTO_POLICY,
                FieldKey.FUNDING_ROUND,
                FieldKey.PUBLIC_STATUS,
            },
        )
        row = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.RTO_POLICY
        )
        self.assertEqual(row.state, State.ACCEPTED)
        self.assertEqual(row.value_text, "Remote first")
        self.assertEqual(row.source_url, "https://jobs.example.test/about")
        # Attribution comes from the host actually fetched, never from what
        # the page claims about itself.
        self.assertEqual(row.source_domain, "jobs.example.test")
        self.assertEqual(row.scope_json, {"claimed_domain": "example.test"})
        self.assertEqual(row.observation, observation)
        self.assertEqual(row.observed_at, observation.observed_at)
        self.assertEqual(row.validation_version, observation.extraction_version)
        self.assertEqual(row.extractor_version, observation.extraction_version)
        # First accept: the value appears now, so it changed now.
        self.assertEqual(row.last_changed_at, now)
        self.assertEqual(row.last_checked_at, now)
        self.assertEqual(row.last_successful_fetch_at, now)
        self.assertEqual(row.last_verified_at, now)

    def test_accept_from_accepted_status_creates_rows(self):
        observation = make_observation(self.organization, status=Status.ACCEPTED)
        created = accept_observation_fields(observation)
        self.assertTrue(created)
        self.assertEqual(
            CompanyFieldEvidence.objects.filter(state=State.ACCEPTED).count(),
            len(created),
        )

    def test_second_accept_with_same_value_supersedes_without_change_claim(self):
        first_at = timezone.now() - timedelta(days=10)
        first = make_observation(
            self.organization, status=Status.AUTO_APPLIED, observed_at=first_at,
            fingerprint="fp-first",
        )
        accept_observation_fields(first, now=first_at)
        prior = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.RTO_POLICY
        )
        second_at = timezone.now()
        second = make_observation(
            self.organization, status=Status.AUTO_APPLIED, observed_at=second_at,
            fingerprint="fp-second",
        )

        accept_observation_fields(second, now=second_at)

        prior.refresh_from_db()
        self.assertEqual(prior.state, State.SUPERSEDED)
        accepted = CompanyFieldEvidence.objects.filter(
            organization=self.organization,
            field_key=FieldKey.RTO_POLICY,
            state=State.ACCEPTED,
        )
        self.assertEqual(accepted.count(), 1)
        replacement = accepted.get()
        self.assertEqual(replacement.observation, second)
        # Same value: no spurious change claim — the new row inherits the
        # prior row's last_changed_at.
        self.assertEqual(replacement.last_changed_at, prior.last_changed_at)
        self.assertEqual(replacement.last_verified_at, second_at)

    def test_second_accept_with_different_value_advances_last_changed(self):
        first_at = timezone.now() - timedelta(days=10)
        first = make_observation(
            self.organization, status=Status.AUTO_APPLIED, observed_at=first_at,
            fingerprint="fp-first",
        )
        accept_observation_fields(first, now=first_at)
        prior = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.RTO_POLICY
        )
        second_at = timezone.now()
        second = make_observation(
            self.organization, status=Status.AUTO_APPLIED, observed_at=second_at,
            rto_evidence="Hybrid 3 days", fingerprint="fp-second",
        )

        accept_observation_fields(second, now=second_at)

        prior.refresh_from_db()
        self.assertEqual(prior.state, State.SUPERSEDED)
        replacement = CompanyFieldEvidence.objects.get(
            organization=self.organization,
            field_key=FieldKey.RTO_POLICY,
            state=State.ACCEPTED,
        )
        self.assertEqual(replacement.value_text, "Hybrid 3 days")
        self.assertEqual(replacement.last_changed_at, second_at)
        self.assertGreater(replacement.last_changed_at, prior.last_changed_at)

    def test_accept_records_one_publication_event(self):
        observation = make_observation(self.organization, status=Status.AUTO_APPLIED)

        accept_observation_fields(observation)

        event = PublicationEvent.objects.get()
        self.assertEqual(event.target_type, PublicationEvent.TargetType.ORGANIZATION)
        self.assertEqual(event.target_id, self.organization.pk)
        self.assertEqual(event.event_kind, PublicationEvent.EventKind.OBSERVED)
        self.assertEqual(event.payload["observation_id"], observation.pk)
        # Same payload shape as the crawler's non-evidence fallback event, so
        # the ORGANIZATION/OBSERVED outbox contract does not vary by path.
        self.assertEqual(event.payload["status"], observation.status)
        self.assertEqual(
            CompanyFieldEvidence.objects.filter(state=State.ACCEPTED).count(), 6
        )

    def test_observation_with_no_carried_fields_records_no_event(self):
        observation = make_observation(
            self.organization,
            status=Status.AUTO_APPLIED,
            observed_name="",
            observed_domain="",
            locations=[],
            rto_evidence="",
            funding_evidence="",
            public_status_evidence="",
        )
        created = accept_observation_fields(observation)
        self.assertEqual(created, [])
        self.assertEqual(PublicationEvent.objects.count(), 0)

    def test_page_claimed_domain_never_becomes_the_attribution(self):
        # The crawled page claims an authoritative-looking domain for itself.
        # source_url is allowlist-checked against the fetch host; the claim
        # is not, so it must never be presented as provenance.
        observation = make_observation(
            self.organization,
            status=Status.AUTO_APPLIED,
            source_url="https://sketchy-aggregator.example/acme",
            observed_domain="sec.gov",
        )

        accept_observation_fields(observation)

        for row in CompanyFieldEvidence.objects.filter(state=State.ACCEPTED):
            self.assertEqual(row.source_domain, "sketchy-aggregator.example")
            self.assertNotEqual(row.source_domain, "sec.gov")
            self.assertEqual(row.scope_json, {"claimed_domain": "sec.gov"})

    def test_accept_heals_duplicate_accepted_rows_to_exactly_one(self):
        # Two accepted rows for one field, as an earlier unserialized race
        # could have left behind. The next legitimate accept must reconcile
        # them, not supersede one and add another.
        for index in range(2):
            CompanyFieldEvidence.objects.create(
                organization=self.organization,
                field_key=FieldKey.RTO_POLICY,
                value_text=f"Racer {index}",
                source_url="https://jobs.example.test/about",
                source_domain="jobs.example.test",
                observed_at=timezone.now() - timedelta(days=2 + index),
                validation_version="v1",
                extractor_version="v1",
                state=State.ACCEPTED,
            )
        observation = make_observation(
            self.organization, status=Status.AUTO_APPLIED, fingerprint="fp-heal"
        )

        accept_observation_fields(observation)

        accepted = CompanyFieldEvidence.objects.filter(
            organization=self.organization,
            field_key=FieldKey.RTO_POLICY,
            state=State.ACCEPTED,
        )
        self.assertEqual(accepted.count(), 1)
        self.assertEqual(accepted.get().observation, observation)
        self.assertEqual(
            CompanyFieldEvidence.objects.filter(
                field_key=FieldKey.RTO_POLICY, state=State.SUPERSEDED
            ).count(),
            2,
        )


class RecordCheckTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Example Labs", url="https://example.test"
        )
        observation = make_observation(self.organization, status=Status.AUTO_APPLIED)
        self.accepted_at = timezone.now() - timedelta(days=5)
        accept_observation_fields(observation, now=self.accepted_at)

    def _row(self):
        return CompanyFieldEvidence.objects.get(
            organization=self.organization,
            field_key=FieldKey.RTO_POLICY,
            state=State.ACCEPTED,
        )

    def test_failed_check_moves_only_last_checked(self):
        before = self._row()
        checked_at = timezone.now()

        row = record_check(
            self.organization, FieldKey.RTO_POLICY, success=False, now=checked_at
        )

        self.assertIsNotNone(row)
        self.assertEqual(row.last_checked_at, checked_at)
        self.assertEqual(row.last_successful_fetch_at, before.last_successful_fetch_at)
        self.assertEqual(row.last_changed_at, before.last_changed_at)
        self.assertEqual(row.last_verified_at, before.last_verified_at)
        self.assertEqual(row.value_text, before.value_text)

    def test_successful_unverified_check_moves_checked_and_fetched_only(self):
        before = self._row()
        checked_at = timezone.now()

        row = record_check(
            self.organization,
            FieldKey.RTO_POLICY,
            success=True,
            verified=False,
            now=checked_at,
        )

        self.assertEqual(row.last_checked_at, checked_at)
        self.assertEqual(row.last_successful_fetch_at, checked_at)
        self.assertEqual(row.last_verified_at, before.last_verified_at)
        self.assertEqual(row.last_changed_at, before.last_changed_at)

    def test_verified_check_with_same_value_does_not_claim_change(self):
        before = self._row()
        checked_at = timezone.now()

        row = record_check(
            self.organization,
            FieldKey.RTO_POLICY,
            success=True,
            value="Remote first",
            verified=True,
            now=checked_at,
        )

        self.assertEqual(row.last_verified_at, checked_at)
        self.assertEqual(row.last_changed_at, before.last_changed_at)
        self.assertEqual(row.value_text, "Remote first")

    def test_verified_check_with_different_value_moves_changed_and_verified(self):
        before = self._row()
        checked_at = timezone.now()

        row = record_check(
            self.organization,
            FieldKey.RTO_POLICY,
            success=True,
            value="Hybrid 2 days",
            verified=True,
            accept_value=True,
            now=checked_at,
        )

        self.assertEqual(row.last_verified_at, checked_at)
        self.assertEqual(row.last_changed_at, checked_at)
        self.assertEqual(row.value_text, "Hybrid 2 days")
        self.assertGreater(row.last_changed_at, before.last_changed_at)

    def test_value_is_ignored_unless_the_caller_accepts_it(self):
        # AC-2: freshness is recordable from any fetch, but the accepted
        # value may only be rewritten by a caller that has established
        # acceptance. Without accept_value the value is inert.
        before = self._row()
        checked_at = timezone.now()

        row = record_check(
            self.organization,
            FieldKey.RTO_POLICY,
            success=True,
            value="Five days in office, no exceptions",
            verified=False,
            now=checked_at,
        )

        self.assertEqual(row.value_text, before.value_text)
        self.assertEqual(row.last_changed_at, before.last_changed_at)
        self.assertEqual(row.last_verified_at, before.last_verified_at)
        self.assertEqual(row.last_checked_at, checked_at)
        self.assertEqual(row.last_successful_fetch_at, checked_at)

    def test_record_check_without_accepted_evidence_returns_none(self):
        result = record_check(
            self.organization,
            FieldKey.ACCELERATED_VESTING,
            success=True,
            value="yes",
            verified=True,
            accept_value=True,
        )
        self.assertIsNone(result)
        self.assertEqual(
            CompanyFieldEvidence.objects.filter(
                field_key=FieldKey.ACCELERATED_VESTING
            ).count(),
            0,
        )


class ResolveFieldEvidenceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Example Labs", url="https://example.test"
        )

    def _evidence(self, field_key, state, observed_at, **kwargs):
        values = {
            "organization": self.organization,
            "field_key": field_key,
            "value_text": "value",
            "source_url": "https://jobs.example.test/about",
            "observed_at": observed_at,
            "validation_version": "v1",
            "extractor_version": "v1",
            "state": state,
        }
        values.update(kwargs)
        return CompanyFieldEvidence.objects.create(**values)

    def test_ignores_superseded_and_conflicted_rows(self):
        now = timezone.now()
        self._evidence(FieldKey.RTO_POLICY, State.SUPERSEDED, now)
        self._evidence(FieldKey.FUNDING_ROUND, State.CONFLICTED, now)

        resolved = resolve_field_evidence(self.organization)

        self.assertEqual(resolved, {})

    def test_newest_observed_at_wins_for_duplicate_accepted_rows(self):
        older = self._evidence(
            FieldKey.RTO_POLICY,
            State.ACCEPTED,
            timezone.now() - timedelta(days=30),
            value_text="older",
        )
        newer = self._evidence(
            FieldKey.RTO_POLICY, State.ACCEPTED, timezone.now(), value_text="newer"
        )

        resolved = resolve_field_evidence(self.organization)

        self.assertEqual(resolved[FieldKey.RTO_POLICY].pk, newer.pk)
        self.assertNotEqual(resolved[FieldKey.RTO_POLICY].pk, older.pk)


class StalenessAndPayloadTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Example Labs", url="https://example.test"
        )

    def _accepted(self, field_key, *, last_verified_at=None, **kwargs):
        values = {
            "organization": self.organization,
            "field_key": field_key,
            "value_text": "value",
            "source_url": "https://jobs.example.test/about",
            "source_domain": "example.test",
            "observed_at": timezone.now(),
            "validation_version": "v1",
            "extractor_version": "v1",
            "state": State.ACCEPTED,
            "scope_json": {"countries": ["US"]},
            "last_verified_at": last_verified_at,
        }
        values.update(kwargs)
        return CompanyFieldEvidence.objects.create(**values)

    def test_is_stale_within_policy(self):
        now = timezone.now()
        verified = now - timedelta(days=FIELD_FRESHNESS_POLICY[FieldKey.RTO_POLICY] - 1)
        self.assertFalse(is_stale(verified, now=now, field_key=FieldKey.RTO_POLICY))

    def test_is_stale_beyond_policy(self):
        now = timezone.now()
        verified = now - timedelta(days=FIELD_FRESHNESS_POLICY[FieldKey.RTO_POLICY] + 1)
        self.assertTrue(is_stale(verified, now=now, field_key=FieldKey.RTO_POLICY))

    def test_is_stale_when_never_verified(self):
        self.assertTrue(is_stale(None, field_key=FieldKey.RTO_POLICY))

    def test_unknown_field_key_uses_default_freshness_days(self):
        now = timezone.now()
        within = now - timedelta(days=DEFAULT_FRESHNESS_DAYS - 1)
        beyond = now - timedelta(days=DEFAULT_FRESHNESS_DAYS + 1)
        self.assertFalse(is_stale(within, now=now, field_key="not_a_registered_key"))
        self.assertTrue(is_stale(beyond, now=now, field_key="not_a_registered_key"))

    def test_payload_marks_fresh_and_stale_fields_and_unverified_keys(self):
        now = timezone.now()
        self._accepted(
            FieldKey.RTO_POLICY,
            last_verified_at=now - timedelta(days=1),
            last_checked_at=now,
            last_successful_fetch_at=now,
            last_changed_at=now - timedelta(days=1),
        )
        self._accepted(
            FieldKey.FUNDING_ROUND,
            last_verified_at=now
            - timedelta(days=FIELD_FRESHNESS_POLICY[FieldKey.FUNDING_ROUND] + 10),
        )

        payload = field_evidence_payload(self.organization, now=now)

        by_key = {entry["field_key"]: entry for entry in payload["fields"]}
        self.assertEqual(set(by_key), {FieldKey.RTO_POLICY, FieldKey.FUNDING_ROUND})
        fresh = by_key[FieldKey.RTO_POLICY]
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["state"], State.ACCEPTED)
        self.assertEqual(fresh["source_domain"], "example.test")
        self.assertEqual(fresh["scope"], {"countries": ["US"]})
        self.assertIsNotNone(fresh["observed_at"])
        self.assertIsNotNone(fresh["last_checked_at"])
        self.assertIsNotNone(fresh["last_successful_fetch_at"])
        self.assertIsNotNone(fresh["last_changed_at"])
        self.assertIsNotNone(fresh["last_verified_at"])
        self.assertTrue(by_key[FieldKey.FUNDING_ROUND]["stale"])
        self.assertEqual(
            set(payload["unverified_fields"]),
            set(FieldKey.values) - {FieldKey.RTO_POLICY, FieldKey.FUNDING_ROUND},
        )

    def test_payload_without_evidence_lists_every_key_unverified(self):
        payload = field_evidence_payload(self.organization)
        self.assertEqual(payload["fields"], [])
        self.assertEqual(set(payload["unverified_fields"]), set(FieldKey.values))

    def test_never_verified_field_is_stale(self):
        self._accepted(FieldKey.LOCATIONS, last_verified_at=None)
        payload = field_evidence_payload(self.organization)
        self.assertTrue(payload["fields"][0]["stale"])
        self.assertIsNone(payload["fields"][0]["last_verified_at"])


class FieldKeyChoiceTests(TestCase):
    def test_field_keys_exclude_rating_and_score_keys(self):
        for key in FieldKey.values:
            lowered = key.lower()
            self.assertNotIn("score", lowered)
            self.assertNotIn("rating", lowered)
        self.assertEqual(
            set(FieldKey.values),
            {
                "rto_policy",
                "funding_round",
                "public_status",
                "accelerated_vesting",
                "locations",
                "company_name",
                "company_domain",
            },
        )


class ResolveFieldEvidenceForOrgsTests(TestCase):
    """The bulk read helper returns the same rows as per-org resolution."""

    def setUp(self):
        self.org_a = Organization.objects.create(name="OrgA")
        self.org_b = Organization.objects.create(name="OrgB")
        self.ev_a = make_observation(
            self.org_a, rto_evidence="Remote", funding_evidence="Seed",
        )
        self.ev_b = make_observation(self.org_b, rto_evidence="Hybrid")

    def test_bulk_returns_same_as_per_org(self):
        from crank.services.company_evidence import accept_observation_fields

        accept_observation_fields(self.ev_a)
        accept_observation_fields(self.ev_b)

        bulk = resolve_field_evidence_for_orgs([self.org_a.pk, self.org_b.pk])
        for org in (self.org_a, self.org_b):
            per_org = resolve_field_evidence(org)
            assert set(bulk.get(org.pk, {}).keys()) == set(per_org.keys())

    def test_bulk_empty_for_no_ids(self):
        assert resolve_field_evidence_for_orgs([]) == {}


class AcceptCorrectionTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        self.org = Organization.objects.create(name="Acme", status=1)
        self.user = User.objects.create_user(username="rev", password="pw-477-xyz")
        self.now = timezone.now()
        self.old = CompanyFieldEvidence.objects.create(
            organization=self.org,
            field_key="rto_policy",
            value_text="Remote-first",
            source_url="https://acme.example.com/old",
            observed_at=self.now - timedelta(days=30),
            validation_version="v",
            extractor_version="v",
            last_changed_at=self.now - timedelta(days=30),
        )

    def correction(self, **overrides):
        import uuid

        from crank.models.company_correction import CompanyCorrection

        live = resolve_field_evidence(self.org).get(overrides.get("field_key", "rto_policy"))
        values = dict(
            requester=self.user,
            organization=self.org,
            field_key="rto_policy",
            proposed_value="Hybrid",
            evidence_url="https://Careers.Acme.example.com/rto",
            idempotency_key=uuid.uuid4(),
            current_evidence=live,
            current_value=live.value_text if live else "",
        )
        values.update(overrides)
        return CompanyCorrection.objects.create(**values)

    def test_company_scope_supersedes_and_creates_accepted_row(self):
        item = self.correction()
        evidence = accept_correction(item, reviewer=self.user, now=self.now)
        self.old.refresh_from_db()
        self.assertEqual(self.old.state, State.SUPERSEDED)
        self.assertEqual(evidence.state, State.ACCEPTED)
        self.assertEqual(evidence.value_text, "Hybrid")
        self.assertEqual(evidence.source_domain, "careers.acme.example.com")
        self.assertEqual(evidence.scope_json, {})
        self.assertIsNone(evidence.observation)
        self.assertIsNone(evidence.last_successful_fetch_at)
        self.assertEqual(evidence.last_changed_at, self.now)
        self.assertEqual(evidence.last_verified_at, self.now)
        self.assertEqual(evidence.validation_version, "manual-correction.v1")
        item.refresh_from_db()
        self.assertEqual(item.status, "accepted")
        self.assertEqual(item.reviewed_by, self.user)
        self.assertEqual(resolve_field_evidence(self.org)["rto_policy"], evidence)
        event = PublicationEvent.objects.get()
        self.assertEqual(event.event_kind, PublicationEvent.EventKind.CHANGED)
        self.assertEqual(event.payload, {"status": "accepted"})

    def test_scope_mapping(self):
        CompanyFieldEvidence.objects.filter(pk=self.old.pk).update(state=State.SUPERSEDED)
        location = accept_correction(
            self.correction(scope_level="location", scope_value="Germany"), reviewer=self.user
        )
        self.assertEqual(location.scope_json, {"countries": ["Germany"]})
        role = accept_correction(
            self.correction(field_key="funding_round", scope_level="role", scope_value="engineering"),
            reviewer=self.user,
        )
        self.assertEqual(role.scope_json, {"role_families": ["engineering"]})

    def test_scoped_accept_never_replaces_the_company_wide_fact(self):
        from types import SimpleNamespace

        from crank.agents.jobs.matching import _evidence_in_scope

        for level, value, listing in (
            ("location", "London", SimpleNamespace(location_text="New York, NY", title="Engineer")),
            ("role", "design", SimpleNamespace(location_text="London", title="Staff Engineer")),
        ):
            item = self.correction(scope_level=level, scope_value=value)
            with self.assertRaises(CorrectionNotAcceptable) as raised:
                accept_correction(item, reviewer=self.user)
            self.assertIn("scoped correction", str(raised.exception))
            item.refresh_from_db()
            self.assertEqual(item.status, "pending")
            self.old.refresh_from_db()
            self.assertEqual(self.old.state, State.ACCEPTED)
            self.assertTrue(_evidence_in_scope(resolve_field_evidence(self.org)["rto_policy"], listing))
        self.assertEqual(PublicationEvent.objects.count(), 0)

    def test_scoped_accept_without_company_wide_fact_matches_only_in_scope(self):
        from types import SimpleNamespace

        from crank.agents.jobs.matching import _evidence_in_scope

        CompanyFieldEvidence.objects.filter(pk=self.old.pk).update(state=State.SUPERSEDED)
        london = accept_correction(
            self.correction(scope_level="location", scope_value="London"), reviewer=self.user
        )
        self.assertTrue(_evidence_in_scope(london, SimpleNamespace(location_text="London, UK", title="x")))
        self.assertFalse(_evidence_in_scope(london, SimpleNamespace(location_text="New York, NY", title="x")))
        role = accept_correction(
            self.correction(field_key="funding_round", scope_level="role", scope_value="engineer"),
            reviewer=self.user,
        )
        self.assertTrue(_evidence_in_scope(role, SimpleNamespace(location_text="", title="Staff Engineer")))
        self.assertFalse(_evidence_in_scope(role, SimpleNamespace(location_text="", title="Designer")))

    def test_scoped_accept_supersedes_only_the_same_scope(self):
        CompanyFieldEvidence.objects.filter(pk=self.old.pk).update(state=State.SUPERSEDED)
        first = accept_correction(
            self.correction(scope_level="location", scope_value="London", proposed_value="Hybrid"),
            reviewer=self.user,
        )
        second = accept_correction(
            self.correction(scope_level="location", scope_value="London", proposed_value="Remote"),
            reviewer=self.user,
        )
        first.refresh_from_db()
        self.assertEqual(first.state, State.SUPERSEDED)
        self.assertEqual(resolve_field_evidence(self.org)["rto_policy"], second)
        other = self.correction(scope_level="location", scope_value="Paris", proposed_value="Onsite")
        with self.assertRaises(CorrectionNotAcceptable):
            accept_correction(other, reviewer=self.user)

    def test_auto_applied_crawl_does_not_overwrite_a_manual_correction(self):
        manual = accept_correction(self.correction(proposed_value="Hybrid 3 days"), reviewer=self.user)
        observation = make_observation(self.org, rto_evidence="Remote first", funding_evidence="Series A")
        created = accept_observation_fields(observation)
        self.assertNotIn(FieldKey.RTO_POLICY, {row.field_key for row in created})
        self.assertIn(FieldKey.FUNDING_ROUND, {row.field_key for row in created})
        manual.refresh_from_db()
        self.assertEqual(manual.state, State.ACCEPTED)
        self.assertEqual(resolve_field_evidence(self.org)["rto_policy"].value_text, "Hybrid 3 days")
        before = (manual.last_checked_at, manual.last_successful_fetch_at)
        record_check(self.org, "rto_policy", success=True, verified=True)
        manual.refresh_from_db()
        self.assertEqual((manual.last_checked_at, manual.last_successful_fetch_at), before)
        self.assertIsNone(manual.last_successful_fetch_at)

    def test_crawl_with_a_changed_value_flags_the_observation_conflicted(self):
        manual = accept_correction(self.correction(proposed_value="Hybrid 3 days"), reviewer=self.user)
        observation = make_observation(self.org, rto_evidence="Fully onsite")
        created = accept_observation_fields(observation)
        self.assertNotIn(FieldKey.RTO_POLICY, {row.field_key for row in created})
        observation.refresh_from_db()
        self.assertEqual(observation.status, Status.CONFLICTED)
        self.assertIn("rto_evidence", observation.conflict_fields)
        manual.refresh_from_db()
        self.assertEqual(manual.state, State.ACCEPTED)
        self.assertEqual(manual.value_text, "Hybrid 3 days")

    def test_crawl_confirming_a_manual_value_refreshes_its_freshness(self):
        manual = accept_correction(self.correction(proposed_value="Hybrid 3 days"), reviewer=self.user)
        manual_verified = manual.last_verified_at
        later = timezone.now() + timedelta(days=80)
        observation = make_observation(self.org, rto_evidence="  hybrid   3 DAYS ")
        accept_observation_fields(observation, now=later)
        observation.refresh_from_db()
        self.assertEqual(observation.status, Status.AUTO_APPLIED)
        manual.refresh_from_db()
        self.assertEqual(manual.state, State.ACCEPTED)
        self.assertEqual(manual.last_checked_at, later)
        self.assertEqual(manual.last_verified_at, manual_verified)
        self.assertIsNone(manual.last_successful_fetch_at)

    def test_scoped_correction_blocked_matches_what_accept_refuses(self):
        from crank.services.company_evidence import scoped_correction_blocked

        self.assertFalse(scoped_correction_blocked("company", "", self.old))
        self.assertFalse(scoped_correction_blocked("location", "London", None))
        self.assertTrue(scoped_correction_blocked("location", "London", self.old))
        self.assertTrue(scoped_correction_blocked("role", "Engineer", self.old))
        CompanyFieldEvidence.objects.filter(pk=self.old.pk).update(state=State.SUPERSEDED)
        london = accept_correction(
            self.correction(scope_level="location", scope_value="London", proposed_value="Hybrid"),
            reviewer=self.user,
        )
        self.assertFalse(scoped_correction_blocked("location", "London", london))
        self.assertTrue(scoped_correction_blocked("location", "Paris", london))

    def test_operator_accepted_observation_can_replace_a_manual_correction(self):
        manual = accept_correction(self.correction(proposed_value="Hybrid 3 days"), reviewer=self.user)
        observation = make_observation(self.org, status=Status.ACCEPTED, rto_evidence="Remote first")
        created = accept_observation_fields(observation)
        self.assertIn(FieldKey.RTO_POLICY, {row.field_key for row in created})
        manual.refresh_from_db()
        self.assertEqual(manual.state, State.SUPERSEDED)

    def test_displayed_field_values_mirror_the_organization_columns(self):
        from crank.services.company_evidence import displayed_field_values

        self.org.url = "https://www.Acme.example.com/about"
        self.org.rto_policy = Organization.RTOPolicy.HYBRID
        self.org.funding_round = Organization.FundingRound.PUBLIC
        self.org.accelerated_vesting = True
        self.org.save()
        values = displayed_field_values(self.org)
        self.assertEqual(
            values,
            {
                "rto_policy": "Hybrid",
                "funding_round": "Public",
                "accelerated_vesting": "Yes",
                "company_name": "Acme",
                "company_domain": "www.acme.example.com",
            },
        )
        self.org.url = ""
        self.assertNotIn("company_domain", displayed_field_values(self.org))

    def test_first_accept_without_prior_row(self):
        evidence = accept_correction(
            self.correction(field_key="funding_round", proposed_value="Series B"),
            reviewer=self.user,
            now=self.now,
        )
        self.assertEqual(evidence.last_changed_at, self.now)

    def test_equal_value_keeps_last_changed_at(self):
        evidence = accept_correction(
            self.correction(proposed_value="Remote-first"), reviewer=self.user, now=self.now
        )
        self.assertEqual(evidence.last_changed_at, self.old.last_changed_at)

    def test_supersedes_every_accepted_duplicate(self):
        dup = CompanyFieldEvidence.objects.create(
            organization=self.org,
            field_key="rto_policy",
            value_text="Dup",
            source_url="https://acme.example.com/dup",
            observed_at=self.now - timedelta(days=60),
            validation_version="v",
            extractor_version="v",
        )
        accept_correction(self.correction(), reviewer=self.user)
        dup.refresh_from_db()
        self.assertEqual(dup.state, State.SUPERSEDED)
        self.assertEqual(
            CompanyFieldEvidence.objects.filter(
                organization=self.org, field_key="rto_policy", state=State.ACCEPTED
            ).count(),
            1,
        )

    def test_team_scope_refused_and_stays_pending(self):
        item = self.correction(scope_level="team", scope_value="Platform")
        with self.assertRaises(CorrectionNotAcceptable):
            accept_correction(item, reviewer=self.user)
        item.refresh_from_db()
        self.assertEqual(item.status, "pending")
        self.assertEqual(PublicationEvent.objects.count(), 0)

    def test_second_accept_of_same_correction_refused(self):
        item = self.correction()
        accept_correction(item, reviewer=self.user)
        with self.assertRaises(CorrectionNotAcceptable):
            accept_correction(item, reviewer=self.user)

    def test_publication_failure_rolls_back_everything(self):
        item = self.correction()
        with patch(
            "crank.services.company_evidence.publication.record_event",
            side_effect=RuntimeError("outbox down"),
        ):
            with self.assertRaises(RuntimeError):
                accept_correction(item, reviewer=self.user)
        self.old.refresh_from_db()
        self.assertEqual(self.old.state, State.ACCEPTED)
        item.refresh_from_db()
        self.assertEqual(item.status, "pending")
        self.assertEqual(CompanyFieldEvidence.objects.count(), 1)

    def test_stale_snapshot_is_refused_unless_overridden(self):
        first = self.correction(proposed_value="Fully in office")
        stale = self.correction(proposed_value="Hybrid")
        accept_correction(first, reviewer=self.user)
        with self.assertRaises(CorrectionNotAcceptable) as ctx:
            accept_correction(stale, reviewer=self.user)
        self.assertIn("changed since", str(ctx.exception))
        stale.refresh_from_db()
        self.assertEqual(stale.status, "pending")
        evidence = accept_correction(stale, reviewer=self.user, allow_stale=True)
        self.assertEqual(evidence.value_text, "Hybrid")
        self.assertEqual(len(evidence.superseded_ids), 1)

    def test_removed_accepted_fact_is_refused_as_stale(self):
        item = self.correction()
        CompanyFieldEvidence.objects.filter(pk=self.old.pk).update(state=State.SUPERSEDED)
        with self.assertRaises(CorrectionNotAcceptable) as ctx:
            accept_correction(item, reviewer=self.user)
        self.assertIn("removed", str(ctx.exception))

    def test_correction_for_first_fact_has_no_snapshot(self):
        CompanyFieldEvidence.objects.all().delete()
        item = self.correction(current_evidence=None, current_value="")
        evidence = accept_correction(item, reviewer=self.user)
        self.assertEqual(evidence.superseded_ids, [])

    def test_inactive_company_is_refused(self):
        item = self.correction()
        type(self.org).objects.filter(pk=self.org.pk).update(status=0)
        with self.assertRaises(CorrectionNotAcceptable) as ctx:
            accept_correction(item, reviewer=self.user)
        self.assertIn("no longer active", str(ctx.exception))

    def test_hidden_characters_stored_before_validation_are_refused(self):
        for overrides in (
            {"proposed_value": "Hy\u200bbrid"},
            {"evidence_url": "https://exa\u200bmple.com/p"},
            {"scope_level": "role", "scope_value": "Eng\u202e"},
        ):
            item = self.correction(**overrides)
            with self.assertRaises(CorrectionNotAcceptable, msg=str(overrides)):
                accept_correction(item, reviewer=self.user)

    def test_accepted_fact_changed_helper(self):
        from crank.services.company_evidence import accepted_fact_changed

        item = self.correction()
        self.assertFalse(accepted_fact_changed(item, self.old))
        self.assertTrue(accepted_fact_changed(item, None))
        empty = self.correction(current_evidence=None, current_value="")
        self.assertFalse(accepted_fact_changed(empty, None))


class ClaimTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        self.organization = Organization.objects.create(
            name="Example Labs", url="https://example.test"
        )
        self.reviewer = User.objects.create(username="claim-staff")
        self.observation = make_observation(self.organization)

    def _claim(self, value="Remote first", state=State.PENDING, observation=None):
        from crank.services.company_evidence import record_claim

        return record_claim(
            self.organization,
            FieldKey.RTO_POLICY,
            value=value,
            observation=observation or self.observation,
            conflicted=state == State.CONFLICTED,
        )

    def test_claim_is_open_and_never_resolves(self):
        claim = self._claim()

        self.assertEqual(claim.state, State.PENDING)
        self.assertIsNone(claim.last_verified_at)
        self.assertEqual(claim.scope_json, {"claimed_domain": "example.test"})
        self.assertNotIn(FieldKey.RTO_POLICY, resolve_field_evidence(self.organization))

    def test_claim_without_claimed_domain_has_empty_scope(self):
        observation = make_observation(self.organization, observed_domain="")
        self.assertEqual(self._claim(observation=observation).scope_json, {})

    def test_claim_validation(self):
        from crank.services.company_evidence import record_claim

        with pytest.raises(ValueError):
            record_claim(
                self.organization,
                FieldKey.RTO_POLICY,
                observation=self.observation,
                value="",
            )
        with pytest.raises(ValueError):
            record_claim(
                self.organization,
                "not_a_field",
                value="x",
                observation=self.observation,
            )

    def test_repeat_claim_refreshes_and_a_new_value_opens_a_new_claim(self):
        first = self._claim()
        same = self._claim(state=State.CONFLICTED)
        self.assertEqual(same.pk, first.pk)
        self.assertEqual(same.state, State.CONFLICTED)
        self.assertEqual(same.value_text, "Remote first")
        self.assertEqual(same.last_changed_at, first.last_changed_at)

        changed = self._claim(value="Office five days")
        self.assertNotEqual(changed.pk, first.pk)
        first.refresh_from_db()
        self.assertEqual(first.value_text, "Remote first")
        self.assertEqual(first.state, State.SUPERSEDED)
        self.assertEqual(changed.state, State.PENDING)
        self.assertEqual(
            CompanyFieldEvidence.objects.filter(state__in=OPEN_CLAIM_STATES).count(), 1
        )

    def test_rejected_value_is_not_reopened_for_the_same_source(self):
        from crank.services.company_evidence import reject_claim

        claim = self._claim()
        reject_claim(claim, reviewer=self.reviewer)
        self.assertIsNone(self._claim())
        self.assertFalse(
            CompanyFieldEvidence.objects.filter(state__in=OPEN_CLAIM_STATES).exists()
        )
        self.assertIsNotNone(self._claim(value="Hybrid"))

    def test_rejecting_a_plain_claim_publishes_a_changed_event(self):
        from crank.services.company_evidence import reject_claim

        claim = self._claim()
        before = PublicationEvent.objects.count()
        reject_claim(claim, reviewer=self.reviewer)
        event = PublicationEvent.objects.latest("id")
        self.assertEqual(PublicationEvent.objects.count(), before + 1)
        self.assertEqual(event.target_id, self.organization.pk)
        self.assertEqual(event.event_kind, PublicationEvent.EventKind.CHANGED)
        self.assertEqual(event.payload, {"status": "rejected"})

    def test_accept_claim_supersedes_previous_and_emits_event(self):
        from crank.services.company_evidence import accept_claim

        old_obs = make_observation(self.organization, fingerprint="old")
        accept_observation_fields(old_obs)
        old = CompanyFieldEvidence.objects.get(
            field_key=FieldKey.RTO_POLICY, state=State.ACCEPTED
        )
        before = PublicationEvent.objects.count()
        claim = self._claim(value="Hybrid")

        accepted = accept_claim(claim, reviewer=self.reviewer)

        self.assertEqual(accepted.state, State.ACCEPTED)
        self.assertIsNotNone(accepted.last_verified_at)
        old.refresh_from_db()
        self.assertEqual(old.state, State.SUPERSEDED)
        self.assertEqual(PublicationEvent.objects.count(), before + 1)
        self.assertEqual(
            resolve_field_evidence(self.organization)[FieldKey.RTO_POLICY].value_text,
            "Hybrid",
        )

    def test_accept_claim_with_same_value_keeps_last_changed(self):
        from crank.services.company_evidence import accept_claim

        accept_observation_fields(make_observation(self.organization, fingerprint="a"))
        old = CompanyFieldEvidence.objects.get(
            field_key=FieldKey.RTO_POLICY, state=State.ACCEPTED
        )
        accepted = accept_claim(self._claim(), reviewer=self.reviewer)
        self.assertEqual(accepted.last_changed_at, old.last_changed_at)

    def test_accepted_or_rejected_claim_cannot_be_reviewed_again(self):
        from crank.services.company_evidence import accept_claim, reject_claim

        claim = self._claim()
        rejected = reject_claim(claim, reviewer=self.reviewer)
        self.assertEqual(rejected.state, State.REJECTED)
        for action in (accept_claim, reject_claim):
            with pytest.raises(EvidenceNotAcceptable):
                action(claim, reviewer=self.reviewer)
        self.assertNotIn(FieldKey.RTO_POLICY, resolve_field_evidence(self.organization))

    def test_field_key_helper_and_allowlists(self):
        from crank.services.company_evidence import (
            AUTO_APPLY_FIELDS,
            REVIEW_REQUIRED_FIELDS,
            field_key_for_observation_attribute,
        )

        self.assertEqual(
            field_key_for_observation_attribute("rto_evidence"), FieldKey.RTO_POLICY
        )
        self.assertIsNone(field_key_for_observation_attribute("description"))
        self.assertFalse(AUTO_APPLY_FIELDS & REVIEW_REQUIRED_FIELDS)
        self.assertEqual(
            AUTO_APPLY_FIELDS | REVIEW_REQUIRED_FIELDS, set(FieldKey.values)
        )

    def test_field_keys_filter_limits_acceptance(self):
        created = accept_observation_fields(
            make_observation(self.organization, fingerprint="f"),
            field_keys={FieldKey.COMPANY_NAME},
        )
        self.assertEqual([row.field_key for row in created], [FieldKey.COMPANY_NAME])


class ClaimReconciliationTests(TestCase):
    """observe_review_field / accept_claim lifecycle rules (#474 AC6/AC7)."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.organization = Organization.objects.create(
            name="Example Labs", url="https://example.test"
        )
        self.reviewer = User.objects.create(username="reconcile-staff")
        self.observation = make_observation(self.organization)

    def _observe(self, value, *, conflicted=False, reverify=True, observation=None, now=None):
        from crank.services.company_evidence import observe_review_field

        return observe_review_field(
            self.organization,
            FieldKey.RTO_POLICY,
            value=value,
            observation=observation or self.observation,
            conflicted=conflicted,
            reverify=reverify,
            now=now,
        )

    def _open(self):
        return CompanyFieldEvidence.objects.filter(
            field_key=FieldKey.RTO_POLICY, state__in=OPEN_CLAIM_STATES
        )

    def _accept(self, value="Remote first"):
        from crank.services.company_evidence import accept_claim, record_claim

        claim = record_claim(
            self.organization, FieldKey.RTO_POLICY, value=value,
            observation=self.observation,
        )
        return accept_claim(claim, reviewer=self.reviewer)

    def test_row_from_an_operator_accepted_observation_is_staff_reviewed(self):
        from crank.services.company_evidence import is_staff_reviewed

        row = self._accept()
        OperationalChangeAudit.objects.all().delete()
        self.assertFalse(is_staff_reviewed(CompanyFieldEvidence.objects.get(pk=row.pk)))
        CompanyFieldEvidence.objects.filter(pk=row.pk).update(observation=self.observation)
        type(self.observation).objects.filter(pk=self.observation.pk).update(status=Status.ACCEPTED)
        self.assertTrue(is_staff_reviewed(CompanyFieldEvidence.objects.get(pk=row.pk)))

    def test_state_follows_the_accepted_value_not_the_prior_observation(self):
        self.assertEqual(self._observe("Remote first"), "claimed")
        self.assertEqual(self._open().get().state, State.PENDING)
        self.assertEqual(self._observe("Remote first", conflicted=True), "claimed")
        self.assertEqual(self._open().get().state, State.CONFLICTED)
        self._accept("Remote first")
        self.assertEqual(self._observe("Five days in office"), "claimed")
        claim = self._open().get()
        self.assertEqual(claim.state, State.CONFLICTED)
        self.assertEqual(self._observe("Five days in office", conflicted=False), "claimed")
        self.assertEqual(self._open().get().state, State.CONFLICTED)

    def test_reviewed_equal_value_reverifies_and_closes_open_claims(self):
        self._accept("Remote first")
        self._observe("Hybrid, 2 days")
        later = timezone.now() + timedelta(days=100)

        self.assertEqual(self._observe("Remote first", now=later), "verified")

        self.assertFalse(self._open().exists())
        accepted = CompanyFieldEvidence.objects.get(state=State.ACCEPTED, field_key=FieldKey.RTO_POLICY)
        self.assertEqual(accepted.last_verified_at, later)
        self.assertEqual(accepted.value_text, "Remote first")

    def test_reverify_can_be_withheld(self):
        self._accept("Remote first")
        self.assertEqual(self._observe("Remote first", reverify=False), "none")

    def test_legacy_unreviewed_equal_value_gets_a_pending_claim(self):
        accept_observation_fields(make_observation(self.organization, fingerprint="legacy"))
        row = CompanyFieldEvidence.objects.get(field_key=FieldKey.RTO_POLICY, state=State.ACCEPTED)
        verified_before = row.last_verified_at

        self.assertEqual(self._observe("Remote first"), "claimed")

        self.assertEqual(self._open().get().state, State.PENDING)
        row.refresh_from_db()
        self.assertEqual(row.last_verified_at, verified_before)
        self.assertFalse(is_reviewed(row))

    def test_a_dropped_field_and_a_reverted_value_close_stale_claims(self):
        self._observe("Hybrid, 2 days")
        self.assertEqual(self._observe(""), "none")
        self.assertFalse(self._open().exists())
        self._accept("Remote first")
        self._observe("Hybrid, 2 days")
        self._observe("Remote first")
        self.assertFalse(self._open().exists())

    def test_rejected_value_is_not_requeued(self):
        from crank.services.company_evidence import reject_claim

        self._observe("Remote first")
        reject_claim(self._open().get(), reviewer=self.reviewer)
        self.assertEqual(self._observe("Remote first"), "none")
        self.assertFalse(self._open().exists())

    def test_accept_claim_reevaluates_other_open_claims(self):
        from crank.services.company_evidence import accept_claim

        other = make_observation(
            self.organization, source_url="https://other.example.test/about", fingerprint="o"
        )
        self._observe("Remote first")
        self._observe("Hybrid, 3 days", observation=other)
        winner = CompanyFieldEvidence.objects.get(value_text="Remote first", state=State.PENDING)
        accept_claim(winner, reviewer=self.reviewer)
        remaining = self._open().get()
        self.assertEqual(remaining.value_text, "Hybrid, 3 days")
        self.assertEqual(remaining.state, State.CONFLICTED)

    def test_accept_claim_refuses_rejected_observation_and_bad_scope(self):
        from crank.services.company_evidence import accept_claim

        self._observe("Remote first")
        claim = self._open().get()
        self.observation.status = Status.REJECTED
        self.observation.save()
        with pytest.raises(EvidenceNotAcceptable):
            accept_claim(claim, reviewer=self.reviewer)
        self.observation.status = Status.AUTO_APPLIED
        self.observation.save()
        CompanyFieldEvidence.objects.filter(pk=claim.pk).update(scope_json={"teams": ["Payments"]})
        with pytest.raises(EvidenceNotAcceptable, match="Team scope cannot be applied"):
            accept_claim(claim, reviewer=self.reviewer)
        self.assertEqual(self._open().get().state, State.PENDING)

    def test_accept_audits_superseded_ids_and_marks_reviewed(self):
        from crank.models.monitoring import OperationalChangeAudit

        first = self._accept("Remote first")
        second = self._accept("Hybrid, 2 days")
        audit = OperationalChangeAudit.objects.get(
            action="claim_accepted", target_id=str(second.pk)
        )
        self.assertEqual(audit.new_value["superseded"], [first.pk])
        self.assertEqual(audit.old_value["state"], State.CONFLICTED)
        self.assertTrue(is_reviewed(second))

    def test_validate_claim_scope(self):
        from crank.services.company_evidence import validate_claim_scope

        self.assertEqual(
            validate_claim_scope(
                {"countries": ["Germany", "\u200bGermany", "France"], "claimed_domain": " a.test "}
            ),
            {"countries": ["France", "Germany"], "claimed_domain": "a.test"},
        )
        for bad in (
            [], {"teams": []}, {"x": 1}, {"countries": "US"}, {"countries": ["\u200b"]},
            {"countries": ["a"] * 11}, {"role_families": ["a" * 101]}, {"claimed_domain": 3},
        ):
            with pytest.raises(EvidenceNotAcceptable):
                validate_claim_scope(bad)

    def test_update_claim_scope_only_touches_scope_and_refuses_closed_claims(self):
        from crank.services.company_evidence import reject_claim, update_claim_scope

        self._observe("Remote first")
        claim = self._open().get()
        result = update_claim_scope(claim, {"countries": ["Germany"]})
        self.assertEqual(result["new"], {"countries": ["Germany"]})
        claim.refresh_from_db()
        self.assertEqual(claim.scope_json, {"countries": ["Germany"]})
        reject_claim(claim, reviewer=self.reviewer)
        with pytest.raises(EvidenceNotAcceptable):
            update_claim_scope(claim, {"countries": ["France"]})

    def test_resolve_observation_claims_and_scoped_conflicts(self):
        from crank.services.company_evidence import (
            resolve_observation_claims,
            scoped_accepted_conflicts,
        )

        self._observe("Remote first")
        self.assertEqual(len(resolve_observation_claims(self.observation, State.REJECTED)), 1)
        self.assertEqual(self._open().count(), 0)
        with pytest.raises(ValueError):
            resolve_observation_claims(self.observation, State.ACCEPTED)
        unresolved = make_observation(None, fingerprint="u")
        self.assertEqual(resolve_observation_claims(unresolved, State.SUPERSEDED), [])
        self.assertEqual(scoped_accepted_conflicts(unresolved), [])

        accepted = self._accept("Hybrid, 2 days")
        same = make_observation(self.organization, rto_evidence="Hybrid, 2 days", fingerprint="s")
        self.assertEqual(scoped_accepted_conflicts(same), [])
        CompanyFieldEvidence.objects.filter(pk=accepted.pk).update(
            scope_json={"countries": ["Germany"]}
        )
        self.assertEqual(scoped_accepted_conflicts(self.observation), [FieldKey.RTO_POLICY])
        self.assertEqual(scoped_accepted_conflicts(same), [])


def is_reviewed(row):
    from crank.services.company_evidence import is_staff_reviewed

    return is_staff_reviewed(row)


class RoundTwoReviewTests(TestCase):
    """Adversarial-review round 2 fixes for #474."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.organization = Organization.objects.create(
            name="Example Labs", url="https://example.test"
        )
        self.reviewer = User.objects.create(username="r2-staff")

    def _legacy(self, value="Remote first", **fields):
        observation = make_observation(
            self.organization, rto_evidence=value, fingerprint=f"legacy-{value}", **fields
        )
        accept_observation_fields(observation)
        return CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.RTO_POLICY, state=State.ACCEPTED
        ), observation

    def _observe(self, value, observation, **kwargs):
        from crank.services.company_evidence import observe_review_field

        return observe_review_field(
            self.organization, FieldKey.RTO_POLICY, value=value, observation=observation, **kwargs
        )

    def _open(self):
        return CompanyFieldEvidence.objects.filter(
            organization=self.organization,
            field_key=FieldKey.RTO_POLICY,
            state__in=OPEN_CLAIM_STATES,
        )

    def test_rejecting_a_legacy_claim_retracts_the_unreviewed_row(self):
        from crank.services.company_evidence import queue_legacy_claim, reject_claim

        legacy, _ = self._legacy()
        self.assertIn(FieldKey.RTO_POLICY, resolve_field_evidence(self.organization))
        claim = queue_legacy_claim(legacy)
        events = PublicationEvent.objects.count()

        reject_claim(claim, reviewer=self.reviewer)

        legacy.refresh_from_db()
        claim.refresh_from_db()
        self.assertEqual(legacy.state, State.SUPERSEDED)
        self.assertEqual(claim.state, State.REJECTED)
        self.assertNotIn(FieldKey.RTO_POLICY, resolve_field_evidence(self.organization))
        self.assertEqual(PublicationEvent.objects.count(), events + 1)
        audit = OperationalChangeAudit.objects.get(
            action="claim_rejected", target_id=str(claim.pk)
        )
        self.assertEqual(audit.new_value["retracted"], [legacy.pk])

    def test_rejecting_a_claim_never_touches_a_reviewed_row(self):
        from crank.services.company_evidence import (
            accept_claim,
            queue_legacy_claim,
            reject_claim,
        )

        legacy, observation = self._legacy()
        accepted = accept_claim(queue_legacy_claim(legacy), reviewer=self.reviewer)
        self._observe("Hybrid", observation)
        reject_claim(self._open().get(), reviewer=self.reviewer)
        accepted.refresh_from_db()
        self.assertEqual(accepted.state, State.ACCEPTED)

    def test_accepting_an_unchanged_value_preserves_the_scope(self):
        legacy, _ = self._legacy()
        CompanyFieldEvidence.objects.filter(pk=legacy.pk).update(
            scope_json={"countries": ["Germany"], "claimed_domain": "example.test"}
        )
        again = make_observation(self.organization, fingerprint="again")
        created = accept_observation_fields(again)
        row = next(r for r in created if r.field_key == FieldKey.RTO_POLICY)
        self.assertEqual(row.scope_json["countries"], ["Germany"])

    def test_a_different_value_over_a_scoped_row_is_refused_or_skipped(self):
        legacy, _ = self._legacy()
        CompanyFieldEvidence.objects.filter(pk=legacy.pk).update(
            scope_json={"countries": ["Germany"]}
        )
        changed = make_observation(
            self.organization, rto_evidence="Office five days", fingerprint="chg"
        )
        with pytest.raises(EvidenceNotAcceptable):
            accept_observation_fields(changed)
        created = accept_observation_fields(changed, scoped_conflict="skip")
        self.assertNotIn(FieldKey.RTO_POLICY, [r.field_key for r in created])
        legacy.refresh_from_db()
        self.assertEqual(legacy.state, State.ACCEPTED)

    def test_accept_claim_with_the_accepted_value_inherits_its_scope(self):
        from crank.services.company_evidence import accept_claim

        legacy, observation = self._legacy()
        self._observe("Remote first", observation)  # legacy same value -> pending claim
        claim = self._open().get()
        CompanyFieldEvidence.objects.filter(pk=legacy.pk).update(
            scope_json={"countries": ["Germany"]}
        )
        claim.refresh_from_db()
        accepted = accept_claim(claim, reviewer=self.reviewer)
        self.assertEqual(accepted.scope_json.get("countries"), ["Germany"])
        audit = OperationalChangeAudit.objects.get(
            action="claim_accepted", target_id=str(accepted.pk)
        )
        self.assertEqual(audit.new_value["scope_json"].get("countries"), ["Germany"])

    def test_explicit_empty_countries_widens_the_scope(self):
        from crank.services.company_evidence import accepted_scope_for_claim

        legacy, observation = self._legacy()
        CompanyFieldEvidence.objects.filter(pk=legacy.pk).update(
            scope_json={"countries": ["Germany"]}
        )
        legacy.refresh_from_db()
        claim = CompanyFieldEvidence(
            value_text=legacy.value_text, scope_json={"countries": []}
        )
        self.assertEqual(accepted_scope_for_claim(claim, legacy)["countries"], [])
        claim.scope_json = {}
        self.assertEqual(accepted_scope_for_claim(claim, legacy)["countries"], ["Germany"])

    def test_reverting_to_a_rejected_value_still_closes_stale_claims(self):
        from crank.services.company_evidence import reject_claim

        _, observation = self._legacy("Remote first")
        self._observe("Hybrid", observation)
        hybrid = self._open().get()
        reject_claim(hybrid, reviewer=self.reviewer)
        self._observe("Office five days", observation)
        stale = self._open().get()
        self.assertEqual(stale.value_text, "Office five days")

        self._observe("Hybrid", observation)  # the rejected value returns

        self.assertFalse(self._open().exists())
        stale.refresh_from_db()
        self.assertEqual(stale.state, State.SUPERSEDED)

    def test_a_rejection_stops_suppressing_after_the_window(self):
        from crank.services.company_evidence import (
            REJECTION_SUPPRESSION_DAYS,
            reject_claim,
        )

        _, observation = self._legacy("Remote first")
        self._observe("Hybrid", observation)
        rejected = reject_claim(self._open().get(), reviewer=self.reviewer)
        self._observe("Hybrid", observation)
        self.assertFalse(self._open().exists())
        later = timezone.now() + timedelta(days=REJECTION_SUPPRESSION_DAYS + 1)
        self._observe("Hybrid", observation, now=later)
        reopened = self._open().get()
        self.assertEqual(reopened.value_text, "Hybrid")
        self.assertNotEqual(reopened.pk, rejected.pk)

    def test_rejected_value_conflicts_lists_recent_rejections_only(self):
        from crank.services.company_evidence import (
            reject_claim,
            rejected_value_conflicts,
        )

        _, observation = self._legacy("Remote first")
        self._observe("Hybrid", observation)
        rejected = reject_claim(self._open().get(), reviewer=self.reviewer)
        carrier = make_observation(
            self.organization, rto_evidence="Hybrid", fingerprint="carrier"
        )
        self.assertEqual(rejected_value_conflicts(carrier), [FieldKey.RTO_POLICY])
        CompanyFieldEvidence.objects.filter(pk=rejected.pk).update(
            last_checked_at=timezone.now() - timedelta(days=400)
        )
        self.assertEqual(rejected_value_conflicts(carrier), [])

    def test_manual_correction_rows_count_as_staff_reviewed(self):
        from crank.services.company_evidence import is_staff_reviewed

        legacy, _ = self._legacy()
        self.assertFalse(is_staff_reviewed(legacy))
        CompanyFieldEvidence.objects.filter(pk=legacy.pk).update(
            observation=None, validation_version="manual-correction.v1"
        )
        legacy.refresh_from_db()
        self.assertTrue(is_staff_reviewed(legacy))
        self.assertNotIn(legacy.pk, [r.pk for r in self._legacy_rows()])

    def _legacy_rows(self):
        from crank.services.company_evidence import legacy_unreviewed_rows

        return legacy_unreviewed_rows(self.organization)

    def test_legacy_rows_and_queueing_are_idempotent(self):
        from crank.services.company_evidence import queue_legacy_claim

        legacy, _ = self._legacy()
        self.assertIn(legacy.pk, [r.pk for r in self._legacy_rows()])
        events = PublicationEvent.objects.count()
        claim = queue_legacy_claim(legacy)
        self.assertEqual(claim.state, State.PENDING)
        self.assertEqual(PublicationEvent.objects.count(), events + 1)
        self.assertEqual(PublicationEvent.objects.latest("id").payload, {"status": "claim_queued"})
        self.assertEqual(claim.scope_json, {"claimed_domain": "example.test"})
        self.assertIsNone(queue_legacy_claim(legacy))
        self.assertEqual(PublicationEvent.objects.count(), events + 1)
        claim.refresh_from_db()
        self.assertEqual(self._open().count(), 1)

    def test_accepting_a_claim_audits_full_hash_and_closed_claims(self):
        from crank.services.company_evidence import accept_claim, value_digest

        _, observation = self._legacy("Remote first")
        long_value = "Hybrid " + "x" * 400
        other = make_observation(
            self.organization, source_url="https://o.example.test/a", fingerprint="oo",
            rto_evidence=long_value,
        )
        self._observe(long_value, other)
        self._observe(long_value, observation)
        claim = self._open().get(value_text=long_value, source_url=observation.source_url)
        twin = self._open().get(value_text=long_value, source_url=other.source_url)
        accepted = accept_claim(claim, reviewer=self.reviewer)
        audit = OperationalChangeAudit.objects.get(
            action="claim_accepted", target_id=str(accepted.pk)
        )
        self.assertEqual(audit.new_value["value_sha256"], value_digest(long_value))
        self.assertEqual(audit.new_value["value_length"], len(long_value))
        self.assertEqual(audit.new_value["claims_closed"], [twin.pk])

    def test_update_claim_scope_audits_inside_the_transaction_with_the_reviewer(self):
        from crank.services.company_evidence import queue_legacy_claim, update_claim_scope

        legacy, _ = self._legacy()
        claim = queue_legacy_claim(legacy)
        update_claim_scope(claim, {"countries": ["Germany"]}, reviewer=self.reviewer)
        audit = OperationalChangeAudit.objects.get(
            action="scope_change", target_id=str(claim.pk)
        )
        self.assertEqual(audit.actor, self.reviewer)
        self.assertEqual(audit.new_value["scope_json"], {"countries": ["Germany"]})

    def test_record_claim_applies_the_per_field_rules(self):
        from crank.services.company_evidence import record_claim

        legacy, observation = self._legacy("Remote first")
        from crank.services.company_evidence import accept_claim, queue_legacy_claim

        accept_claim(queue_legacy_claim(legacy), reviewer=self.reviewer)
        self.assertIsNone(
            record_claim(
                self.organization, FieldKey.RTO_POLICY, value="Remote first",
                observation=observation,
            )
        )


class RoundThreeReviewTests(TestCase):
    """Adversarial-review round 3 fixes for #474."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.organization = Organization.objects.create(
            name="Example Labs", url="https://example.test"
        )
        self.reviewer = User.objects.create(username="r3-staff")

    def _page(self, path, **fields):
        return make_observation(
            self.organization,
            source_url=f"https://jobs.example.test/{path}",
            fingerprint=f"r3-{path}-{timezone.now().timestamp()}",
            **fields,
        )

    def _open(self, field_key=FieldKey.RTO_POLICY):
        return CompanyFieldEvidence.objects.filter(
            organization=self.organization, field_key=field_key, state__in=OPEN_CLAIM_STATES
        )

    def test_identity_fields_open_no_legacy_claim_and_a_reject_never_retracts_them(self):
        from crank.services.company_evidence import (
            observe_review_field,
            observation_field_values,
            reject_claim,
            resolve_field_evidence,
        )

        about = self._page("about", locations=["Remote"])
        accept_observation_fields(about, field_keys=AUTO_APPLY_FIELDS)
        accepted = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.LOCATIONS, state=State.ACCEPTED
        )
        team = self._page("team", locations=["Berlin, Germany"], status=Status.CONFLICTED)
        # A conflicting page repeating the accepted value opens nothing.
        outcome = observe_review_field(
            self.organization, FieldKey.LOCATIONS,
            value=observation_field_values(about)[FieldKey.LOCATIONS],
            observation=self._page("again", locations=["Remote"], status=Status.CONFLICTED),
            conflicted=True,
        )
        self.assertEqual(outcome, "none")
        self.assertFalse(self._open(FieldKey.LOCATIONS).exists())
        # A different value is still a (conflicted) claim, as AC7 intends.
        observe_review_field(
            self.organization, FieldKey.LOCATIONS,
            value=observation_field_values(team)[FieldKey.LOCATIONS],
            observation=team, conflicted=True,
        )
        claim = self._open(FieldKey.LOCATIONS).get()
        self.assertEqual(claim.state, State.CONFLICTED)
        # Even a claim repeating the accepted identity value cannot retract it.
        twin = CompanyFieldEvidence.objects.create(
            organization=self.organization, field_key=FieldKey.LOCATIONS,
            value_text=accepted.value_text, source_url="https://jobs.example.test/x",
            source_domain="jobs.example.test", observed_at=timezone.now(),
            state=State.PENDING,
        )
        reject_claim(twin, reviewer=self.reviewer)
        reject_claim(claim, reviewer=self.reviewer)
        accepted.refresh_from_db()
        self.assertEqual(accepted.state, State.ACCEPTED)
        # Country matching reads this key.
        self.assertIn(FieldKey.LOCATIONS, resolve_field_evidence(self.organization))

    def test_rejecting_a_legacy_claim_reconciles_the_fields_other_claims(self):
        from crank.services.company_evidence import (
            accept_observation_fields as accept_fields,
            queue_legacy_claim,
            record_claim,
            reject_claim,
        )

        legacy_obs = self._page("legacy", rto_evidence="Five days")
        accept_fields(legacy_obs)
        legacy = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.RTO_POLICY, state=State.ACCEPTED
        )
        claim = queue_legacy_claim(legacy)
        twin = record_claim(
            self.organization, FieldKey.RTO_POLICY, value="Five days",
            observation=self._page("b", rto_evidence="Five days"),
        )
        other = record_claim(
            self.organization, FieldKey.RTO_POLICY, value="Remote first",
            observation=self._page("c", rto_evidence="Remote first"), conflicted=True,
        )
        self.assertEqual(other.state, State.CONFLICTED)

        reject_claim(claim, reviewer=self.reviewer)

        twin.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(twin.state, State.SUPERSEDED)
        self.assertEqual(other.state, State.PENDING)
        audit = OperationalChangeAudit.objects.get(
            action="claim_rejected", target_id=str(claim.pk)
        )
        self.assertEqual(audit.new_value["claims_closed"], [twin.pk])
        self.assertEqual(audit.new_value["claims_pending"], [other.pk])

    def test_legacy_queueing_skips_a_page_with_an_open_claim_and_keeps_provenance(self):
        from crank.services.company_evidence import (
            accept_claim,
            accept_observation_fields as accept_fields,
            queue_legacy_claim,
            record_claim,
        )

        observation = self._page("about", rto_evidence="Remote first")
        accept_fields(observation)
        legacy = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.RTO_POLICY, state=State.ACCEPTED
        )
        old = timezone.now() - timedelta(days=700)
        CompanyFieldEvidence.objects.filter(pk=legacy.pk).update(
            last_successful_fetch_at=old, scope_json={"claimed_domain": "example.test"}
        )
        legacy.refresh_from_db()
        record_claim(
            self.organization, FieldKey.RTO_POLICY, value="Hybrid, 3 days",
            observation=observation, conflicted=True,
        )
        self.assertIsNone(queue_legacy_claim(legacy))
        self._open().delete()

        claim = queue_legacy_claim(legacy)
        self.assertEqual(claim.scope_json, {"claimed_domain": "example.test"})
        accepted = accept_claim(claim, reviewer=self.reviewer)
        self.assertEqual(accepted.last_verified_at, old)

    def test_rejecting_an_accepted_observation_withdraws_its_review_required_facts(self):
        from crank.services.company_evidence import (
            is_staff_reviewed,
            resolve_field_evidence,
            retract_observation_facts,
        )

        observation = self._page("about", rto_evidence="Five days, no exceptions")
        rows = accept_observation_fields(observation)
        rto = next(r for r in rows if r.field_key == FieldKey.RTO_POLICY)
        OperationalChangeAudit.objects.create(
            target_type="company_field_evidence", target_id=str(rto.pk),
            action="observation_accepted", old_value={}, new_value={}, confirmed=True,
        )
        observation.status = Status.REJECTED
        observation.save()
        rto = CompanyFieldEvidence.objects.get(pk=rto.pk)
        self.assertFalse(is_staff_reviewed(rto))

        review_ids = {r.pk for r in rows if r.field_key in REVIEW_REQUIRED_FIELDS}
        self.assertEqual(set(retract_observation_facts(observation)), review_ids)
        rto.refresh_from_db()
        self.assertEqual(rto.state, State.SUPERSEDED)
        self.assertNotIn(FieldKey.RTO_POLICY, resolve_field_evidence(self.organization))
        identity = CompanyFieldEvidence.objects.filter(
            observation=observation, field_key__in=AUTO_APPLY_FIELDS, state=State.ACCEPTED
        )
        self.assertTrue(identity.exists())

    def test_retracting_facts_leaves_rows_a_claim_accepted_and_orgless_observations(self):
        from crank.services.company_evidence import accept_claim, retract_observation_facts

        observation = self._page("about", rto_evidence="Remote first")
        claim = CompanyFieldEvidence.objects.create(
            organization=self.organization, field_key=FieldKey.RTO_POLICY,
            value_text="Remote first", source_url=observation.source_url,
            source_domain="jobs.example.test", observation=observation,
            observed_at=timezone.now(), state=State.PENDING,
        )
        accepted = accept_claim(claim, reviewer=self.reviewer)
        self.assertEqual(retract_observation_facts(observation), [])
        accepted.refresh_from_db()
        self.assertEqual(accepted.state, State.ACCEPTED)
        observation.organization = None
        self.assertEqual(retract_observation_facts(observation), [])
