# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for accepted field-level evidence (issue #460)."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
import pytest

from crank.models.company_profile import (
    CompanyFieldEvidence,
    CompanyProfileObservation,
)
from crank.models.organization import Organization
from crank.models.publication import PublicationEvent
from crank.services.company_evidence import (
    DEFAULT_FRESHNESS_DAYS,
    FIELD_FRESHNESS_POLICY,
    OPEN_CLAIM_STATES,
    EvidenceNotAcceptable,
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
            state=state,
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

        for kwargs in (
            {"state": State.ACCEPTED},
            {"state": State.PENDING, "value": ""},
        ):
            params = {"value": "x", "state": State.PENDING, **kwargs}
            with pytest.raises(ValueError):
                record_claim(
                    self.organization,
                    FieldKey.RTO_POLICY,
                    observation=self.observation,
                    **params,
                )
        with pytest.raises(ValueError):
            record_claim(
                self.organization,
                "not_a_field",
                value="x",
                observation=self.observation,
                state=State.PENDING,
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
            observation=self.observation, state=State.PENDING,
        )
        return accept_claim(claim, reviewer=self.reviewer)

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
        self.assertEqual(audit.old_value["state"], State.PENDING)
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
