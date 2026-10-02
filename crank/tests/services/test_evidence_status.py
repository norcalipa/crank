# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Shared evidence vocabulary: status, review, validated links, summaries (#473)."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from crank.models.company_profile import CompanyFieldEvidence
from crank.models.organization import Organization
from crank.services.company_evidence import (
    FIELD_FRESHNESS_POLICY,
    TRACKED_FIELD_COUNT,
    evidence_summaries_for_orgs,
    field_evidence_payload,
    field_status,
    open_claims_by_org,
    safe_source_url,
)

FieldKey = CompanyFieldEvidence.FieldKey
State = CompanyFieldEvidence.State


def make_row(
    organization,
    field_key=FieldKey.RTO_POLICY,
    *,
    state=State.ACCEPTED,
    verified_days_ago=1,
    source_url="https://example.test/about",
    source_domain="example.test",
    value="Remote",
    observed_days_ago=1,
):
    now = timezone.now()
    return CompanyFieldEvidence.objects.create(
        organization=organization,
        field_key=field_key,
        value_text=value,
        source_url=source_url,
        source_domain=source_domain,
        observed_at=now - timedelta(days=observed_days_ago),
        validation_version="v1",
        extractor_version="v1",
        state=state,
        last_verified_at=(
            None
            if verified_days_ago is None
            else now - timedelta(days=verified_days_ago)
        ),
    )


class FieldStatusTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Status Org", url="https://example.test", status=1
        )

    def test_policy_boundary_is_fresh_and_one_day_later_is_stale(self):
        now = timezone.now()
        policy = FIELD_FRESHNESS_POLICY[FieldKey.RTO_POLICY]
        row = make_row(self.org)
        row.last_verified_at = now - timedelta(days=policy)
        self.assertEqual(field_status(row, now=now), "verified")
        row.last_verified_at = now - timedelta(days=policy, seconds=1)
        self.assertEqual(field_status(row, now=now), "stale")

    def test_never_verified_row_is_stale(self):
        row = make_row(self.org, verified_days_ago=None)
        self.assertEqual(field_status(row, now=timezone.now()), "stale")

    def test_default_now_is_used(self):
        self.assertEqual(field_status(make_row(self.org)), "verified")


class SafeSourceUrlTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Link Org", url="https://example.test", status=1
        )

    def _url(self, source_url, source_domain="example.test"):
        row = make_row(self.org, source_url=source_url, source_domain=source_domain)
        return safe_source_url(row)

    def test_https_on_matching_host_or_subdomain_is_returned(self):
        self.assertEqual(
            self._url("https://example.test/about"), "https://example.test/about"
        )
        self.assertEqual(
            self._url("https://careers.example.test/x"),
            "https://careers.example.test/x",
        )

    def test_unsafe_or_off_domain_urls_are_suppressed(self):
        for url in (
            "http://example.test/about",
            "https://evil.test/about",
            "https://notexample.test/about",
            "https://user:pw@example.test/about",
            "https://127.0.0.1/about",
            "https://example.test:8443/about",
            "https://example.test/about#frag",
            "not a url",
        ):
            with self.subTest(url=url):
                self.assertIsNone(self._url(url))

    def test_missing_domain_or_url_is_suppressed(self):
        self.assertIsNone(self._url("https://example.test/about", source_domain=""))
        row = make_row(self.org)
        row.source_url = ""
        self.assertIsNone(safe_source_url(row))


class OpenClaimsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Claim Org", url="https://example.test", status=1
        )
        self.other = Organization.objects.create(
            name="Other Org", url="https://other.test", status=1
        )

    def test_only_pending_and_conflicted_count_and_conflict_wins(self):
        make_row(self.org, FieldKey.RTO_POLICY, state=State.PENDING)
        make_row(self.org, FieldKey.RTO_POLICY, state=State.CONFLICTED)
        make_row(self.org, FieldKey.FUNDING_ROUND, state=State.PENDING)
        make_row(self.org, FieldKey.LOCATIONS, state=State.REJECTED)
        make_row(self.org, FieldKey.COMPANY_NAME, state=State.SUPERSEDED)
        claims = open_claims_by_org([self.org.id, self.other.id])
        self.assertEqual(
            claims,
            {self.org.id: {"rto_policy": "conflicted", "funding_round": "pending"}},
        )

    def test_empty_ids_make_no_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(open_claims_by_org([]), {})


class SummaryTests(TestCase):
    def test_counts_last_verified_and_pending(self):
        fresh = Organization.objects.create(
            name="Fresh", url="https://a.test", status=1
        )
        stale = Organization.objects.create(
            name="Stale", url="https://b.test", status=1
        )
        bare = Organization.objects.create(name="Bare", url="https://c.test", status=1)
        make_row(fresh, FieldKey.RTO_POLICY, verified_days_ago=2)
        make_row(fresh, FieldKey.FUNDING_ROUND, verified_days_ago=5)
        make_row(fresh, FieldKey.LOCATIONS, state=State.PENDING)
        make_row(stale, FieldKey.RTO_POLICY, verified_days_ago=400)
        make_row(stale, FieldKey.FUNDING_ROUND, verified_days_ago=None)
        summaries = evidence_summaries_for_orgs([fresh.id, stale.id, bare.id])
        self.assertEqual(summaries[fresh.id]["verified"], 2)
        self.assertEqual(summaries[fresh.id]["stale"], 0)
        self.assertEqual(summaries[fresh.id]["unknown"], TRACKED_FIELD_COUNT - 2)
        self.assertEqual(summaries[fresh.id]["fact_coverage"], 2)
        self.assertEqual(summaries[fresh.id]["pending_review"], 1)
        self.assertIsNotNone(summaries[fresh.id]["last_verified_at"])
        self.assertEqual(summaries[stale.id]["stale"], 2)
        self.assertEqual(summaries[stale.id]["verified"], 0)
        self.assertIsNotNone(summaries[stale.id]["last_verified_at"])
        self.assertEqual(summaries[bare.id]["unknown"], TRACKED_FIELD_COUNT)
        self.assertIsNone(summaries[bare.id]["last_verified_at"])
        self.assertEqual(summaries[bare.id]["total"], TRACKED_FIELD_COUNT)

    def test_two_queries_for_many_organizations(self):
        orgs = [
            Organization.objects.create(
                name=f"Org {i}", url=f"https://o{i}.test", status=1
            )
            for i in range(50)
        ]
        for org in orgs[:10]:
            make_row(org)
        with self.assertNumQueries(2):
            summaries = evidence_summaries_for_orgs([org.id for org in orgs])
        self.assertEqual(len(summaries), 50)


class PayloadTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Payload Org", url="https://example.test", status=1
        )

    def test_payload_status_review_policy_link_and_pending_block(self):
        make_row(self.org, FieldKey.RTO_POLICY, verified_days_ago=1)
        make_row(self.org, FieldKey.RTO_POLICY, state=State.PENDING, value="In office")
        make_row(
            self.org,
            FieldKey.RTO_POLICY,
            state=State.CONFLICTED,
            value="Hybrid",
            observed_days_ago=0,
        )
        make_row(
            self.org,
            FieldKey.LOCATIONS,
            verified_days_ago=400,
            source_url="https://elsewhere.test/x",
        )
        make_row(
            self.org, FieldKey.FUNDING_ROUND, state=State.PENDING, value="Series B"
        )
        make_row(self.org, FieldKey.COMPANY_NAME, state=State.REJECTED)
        payload = field_evidence_payload(self.org)
        by_key = {f["field_key"]: f for f in payload["fields"]}
        self.assertEqual(by_key["rto_policy"]["status"], "verified")
        self.assertEqual(by_key["rto_policy"]["review"], "conflicted")
        self.assertEqual(by_key["rto_policy"]["policy_days"], 90)
        self.assertEqual(
            by_key["rto_policy"]["source_url"], "https://example.test/about"
        )
        self.assertEqual(by_key["locations"]["status"], "stale")
        self.assertTrue(by_key["locations"]["stale"])
        self.assertEqual(by_key["locations"]["review"], "none")
        self.assertIsNone(by_key["locations"]["source_url"])
        pending = {p["field_key"]: p for p in payload["pending_review"]}
        self.assertEqual(set(pending), {"rto_policy", "funding_round"})
        self.assertEqual(pending["rto_policy"]["review"], "conflicted")
        self.assertEqual(pending["rto_policy"]["observed_value"], "Hybrid")
        self.assertEqual(pending["funding_round"]["review"], "pending")
        self.assertEqual(payload["summary"]["verified"], 1)
        self.assertEqual(payload["summary"]["stale"], 1)
        self.assertEqual(payload["summary"]["pending_review"], 2)
        # Unknown funding_round has no accepted row, only a claim.
        self.assertIn("funding_round", payload["unverified_fields"])
