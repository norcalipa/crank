# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Rankings and provenance carry separate coverage / freshness / evidence status (#473)."""

import json

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from crank.models.company_profile import CompanyFieldEvidence
from crank.models.organization import Organization
from crank.models.score import Score, ScoreAlgorithm, ScoreAlgorithmWeight, ScoreType
from crank.services.company_evidence import EVIDENCE_SCHEMA_VERSION, TRACKED_FIELD_COUNT
from crank.services.scores import (
    algorithm_results_cache_key,
    organization_provenance_api_cache_key,
)
from crank.settings import DEFAULT_ALGORITHM_ID
from crank.tests.services.test_evidence_status import make_row

FieldKey = CompanyFieldEvidence.FieldKey
State = CompanyFieldEvidence.State

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class RankingsEvidenceTests(TestCase):
    def setUp(self):
        cache.clear()
        ScoreAlgorithm.objects.create(
            id=DEFAULT_ALGORITHM_ID,
            name="Algo",
            description_content="test.md",
            status=1,
        )
        cache.set("algorithm_object_list", ScoreAlgorithm.objects.filter(status=1))
        self.type_a = ScoreType.objects.create(name="Culture")
        self.type_b = ScoreType.objects.create(name="Leadership")
        ScoreAlgorithmWeight.objects.create(
            algorithm_id=DEFAULT_ALGORITHM_ID, type_id=self.type_a.id, weight=1.0
        )
        self.scorer = Organization.objects.create(
            name="Scorer", url="https://scorer.test", status=1
        )
        self.full = Organization.objects.create(
            name="Full", url="https://full.test", status=1
        )
        self.half = Organization.objects.create(
            name="Half", url="https://half.test", status=1
        )
        for org in (self.full, self.half):
            Score.objects.create(
                source_id=self.scorer.id,
                target_id=org.id,
                score=4.0,
                type_id=self.type_a.id,
            )
        Score.objects.create(
            source_id=self.scorer.id,
            target_id=self.full.id,
            score=3.0,
            type_id=self.type_b.id,
        )

    def _rows(self):
        response = self.client.get(f"/algo/{DEFAULT_ALGORITHM_ID}/")
        self.assertEqual(response.status_code, 200)
        return {
            row["id"]: row for row in response.context_data["top_organization_list"]
        }

    def test_rows_carry_exact_rating_coverage_and_evidence_summary(self):
        make_row(self.full, FieldKey.RTO_POLICY, verified_days_ago=500)
        make_row(self.full, FieldKey.FUNDING_ROUND, verified_days_ago=500)
        make_row(self.full, FieldKey.LOCATIONS, state=State.CONFLICTED)
        rows = self._rows()
        full, half = rows[self.full.id], rows[self.half.id]
        self.assertEqual(
            (full["rating_dimensions_covered"], full["rating_dimensions_total"]), (2, 2)
        )
        self.assertEqual(
            (half["rating_dimensions_covered"], half["rating_dimensions_total"]), (1, 2)
        )
        # 100% rating coverage with every accepted fact past policy is stale.
        self.assertEqual(full["evidence"]["stale"], 2)
        self.assertEqual(full["evidence"]["verified"], 0)
        self.assertEqual(full["evidence"]["unknown"], TRACKED_FIELD_COUNT - 2)
        self.assertEqual(full["evidence"]["pending_review"], 1)
        self.assertEqual(half["evidence"]["unknown"], TRACKED_FIELD_COUNT)
        self.assertIsNone(half["evidence"]["last_verified_at"])

    def test_extra_queries_are_bounded_and_result_is_cached(self):
        for org in (self.full, self.half, self.scorer):
            make_row(org, FieldKey.RTO_POLICY)
        make_row(self.full, FieldKey.LOCATIONS, state=State.PENDING)
        with CaptureQueriesContext(connection) as ctx:
            rows = self._rows()
        self.assertEqual(len(rows), 2)
        evidence_queries = [
            q["sql"]
            for q in ctx.captured_queries
            if "crank_companyfieldevidence" in q["sql"]
            or 'FROM "crank_scoretype"' in q["sql"]
            or "FROM `crank_scoretype`" in q["sql"]
        ]
        self.assertLessEqual(len(evidence_queries), 3)
        self.assertGreaterEqual(len(evidence_queries), 3)
        cached = cache.get(algorithm_results_cache_key(DEFAULT_ALGORITHM_ID))
        self.assertEqual(len(cached), 2)
        self.assertIn("evidence", cached[0])

    def test_cached_shell_is_auth_neutral(self):
        make_row(self.full, FieldKey.RTO_POLICY)
        anonymous = self.client.get(f"/algo/{DEFAULT_ALGORITHM_ID}/").content
        user = User.objects.create_user("someone", password="pw")
        self.client.force_login(user)
        self.assertEqual(
            self.client.get(f"/algo/{DEFAULT_ALGORITHM_ID}/").content, anonymous
        )

    def test_no_organizations_makes_no_evidence_queries(self):
        Score.objects.all().delete()
        self.assertEqual(self._rows(), {})


@override_settings(CACHES=LOCMEM)
class ProvenanceEvidenceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(
            name="Prov Org", url="https://example.test", status=1
        )

    def _get(self, org=None):
        org = org or self.org
        response = self.client.get(f"/api/organizations/{org.pk}/provenance/")
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def test_new_keys_link_suppression_and_pending_excludes_closed_states(self):
        make_row(self.org, FieldKey.RTO_POLICY)
        make_row(self.org, FieldKey.LOCATIONS, source_url="https://elsewhere.test/x")
        make_row(
            self.org, FieldKey.FUNDING_ROUND, state=State.PENDING, value="Series C"
        )
        make_row(self.org, FieldKey.PUBLIC_STATUS, state=State.REJECTED)
        make_row(self.org, FieldKey.COMPANY_NAME, state=State.SUPERSEDED)
        data = self._get()
        self.assertEqual(data["evidence_schema"], EVIDENCE_SCHEMA_VERSION)
        by_key = {f["field_key"]: f for f in data["fields"]}
        self.assertEqual(
            by_key["rto_policy"]["source_url"], "https://example.test/about"
        )
        self.assertIsNone(by_key["locations"]["source_url"])
        self.assertEqual(
            [p["field_key"] for p in data["pending_review"]], ["funding_round"]
        )
        self.assertEqual(data["summary"]["verified"], 2)

    def test_legacy_cached_payload_is_rebuilt_once(self):
        key = organization_provenance_api_cache_key(self.org.pk)
        cache.set(
            key, {"fields": [], "unverified_fields": [], "organization_id": self.org.pk}
        )
        make_row(self.org, FieldKey.RTO_POLICY)
        data = self._get()
        self.assertEqual(data["evidence_schema"], EVIDENCE_SCHEMA_VERSION)
        self.assertEqual(len(data["fields"]), 1)
        make_row(self.org, FieldKey.FUNDING_ROUND)
        self.assertEqual(len(self._get()["fields"]), 1)

    def test_unknown_or_inactive_organization_is_404(self):
        inactive = Organization.objects.create(
            name="Gone", url="https://gone.test", status=0
        )
        self.assertEqual(
            self.client.get("/api/organizations/999999/provenance/").status_code, 404
        )
        self.assertEqual(
            self.client.get(
                f"/api/organizations/{inactive.pk}/provenance/"
            ).status_code,
            404,
        )

    def test_payload_is_identical_for_two_accounts(self):
        make_row(self.org, FieldKey.RTO_POLICY)
        anonymous = self._get()
        cache.clear()
        self.client.force_login(User.objects.create_user("someone", password="pw"))
        self.assertEqual(self._get(), anonymous)
