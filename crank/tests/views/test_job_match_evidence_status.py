# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Job-match responses carry a read-time evidence status per requirement (issue #473)."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from crank.models.company_profile import CompanyFieldEvidence
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.organization import Organization
from crank.models.preference import UserPreference

FieldKey = CompanyFieldEvidence.FieldKey
State = CompanyFieldEvidence.State


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class JobMatchEvidenceStatusTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.now = timezone.now()
        self.owner = User.objects.create_user("owner", password="secret")
        self.organization = Organization.objects.create(name="Acme", rto_policy="R")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        self.listings = [self._listing(f"Engineer {n}") for n in range(3)]
        UserPreference.objects.create(
            user=self.owner, revision=0,
            preferences={"work_location": {"modes": ["remote"]}},
        )
        self.fresh = self._evidence(FieldKey.RTO_POLICY, "Remote", age_days=2)
        self.stale = self._evidence(FieldKey.FUNDING_ROUND, "Series B", age_days=400,
                                    state=State.SUPERSEDED)
        self.client.force_login(self.owner)

    def _listing(self, title):
        slug = title.lower().replace(" ", "-")
        return JobListing.all_objects.create(
            source=self.source,
            external_id=slug,
            canonical_url=f"https://jobs.example.test/{slug}",
            employer_name=self.organization.name,
            title=title,
            first_seen_at=self.now - timedelta(days=1),
            last_seen_at=self.now,
            status=JobListing.Status.ACTIVE,
            organization=self.organization,
        )

    def _evidence(self, field_key, value_text, *, age_days, state=State.ACCEPTED):
        verified_at = self.now - timedelta(days=age_days)
        return CompanyFieldEvidence.objects.create(
            organization=self.organization,
            field_key=field_key,
            value_text=value_text,
            source_url="https://acme.example/about",
            source_domain="acme.example",
            observed_at=verified_at,
            last_verified_at=verified_at,
            validation_version="v1",
            extractor_version="v1",
            state=state,
        )

    def _requirements(self, evidence_backed=True):
        if not evidence_backed:
            return [{
                "path": "work_location.modes", "status": "match", "observed": "remote",
                "source_kind": "field", "source_id": "organization.rto_policy",
                "scope_ok": True,
            }]
        return [
            {
                "path": "work_location.modes", "status": "match", "observed": "remote",
                "source_kind": "evidence", "source_id": self.fresh.pk, "scope_ok": True,
            },
            {
                "path": "funding_stage", "status": "match", "observed": "b",
                "source_kind": "evidence", "source_id": self.stale.pk, "scope_ok": True,
            },
        ]

    def _match(self, listing, *, evidence_backed=True):
        return JobMatch.objects.create(
            user=self.owner,
            listing=listing,
            organization=self.organization,
            preference_version=1,
            ranker_version="1.0.0",
            score=50,
            requirements=self._requirements(evidence_backed),
            first_matched_at=self.now,
            last_matched_at=self.now,
        )

    def _count(self, url):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(captured), response.json()

    def test_list_annotates_every_match_with_one_extra_query(self):
        for listing in self.listings:
            self._match(listing, evidence_backed=False)
        baseline, payload = self._count("/api/job-matches/")
        self.assertEqual(
            payload["results"][0]["requirements"][0]["evidence_status"],
            {"state": "profile", "last_verified_at": None, "source_domain": None},
        )

        JobMatch.objects.all().delete()
        matches = [self._match(listing) for listing in self.listings]
        with_evidence, payload = self._count("/api/job-matches/")

        # One bulk evidence read for the whole page, however many matches.
        self.assertEqual(with_evidence, baseline + 1)
        with self.assertNumQueries(baseline + 1):
            self.client.get("/api/job-matches/")
        self.assertEqual(len(payload["results"]), 3)
        for result in payload["results"]:
            work_mode, funding = result["requirements"]
            self.assertEqual(
                work_mode["evidence_status"],
                {
                    "state": "verified",
                    "last_verified_at": self.fresh.last_verified_at.isoformat(),
                    "source_domain": "acme.example",
                },
            )
            self.assertEqual(
                funding["evidence_status"],
                {"state": "superseded", "last_verified_at": None, "source_domain": None},
            )
        # Status is derived per response; the stored requirements are untouched.
        for match in matches:
            match.refresh_from_db()
            self.assertEqual(match.requirements, self._requirements())

    def test_detail_adds_one_query_and_reports_stale(self):
        plain = self._match(self.listings[0], evidence_backed=False)
        baseline, _ = self._count(f"/api/job-matches/{plain.pk}/")

        match = self._match(self.listings[1])
        CompanyFieldEvidence.objects.filter(pk=self.fresh.pk).update(
            last_verified_at=self.now - timedelta(days=200)
        )
        count, payload = self._count(f"/api/job-matches/{match.pk}/")

        self.assertEqual(count, baseline + 1)
        self.assertEqual(payload["requirements"][0]["evidence_status"]["state"], "stale")
        self.assertEqual(payload["requirements"][0]["status"], "match")
        self.assertIn("factors", payload)

    def test_deleted_evidence_row_reads_missing(self):
        match = self._match(self.listings[0])
        CompanyFieldEvidence.objects.filter(pk=self.fresh.pk).delete()
        payload = self.client.get(f"/api/job-matches/{match.pk}/").json()
        self.assertEqual(
            payload["requirements"][0]["evidence_status"],
            {"state": "missing", "last_verified_at": None, "source_domain": None},
        )

    def test_ranked_annotates_jobs_and_organizations_with_one_extra_query(self):
        CompanyFieldEvidence.objects.all().delete()
        baseline, payload = self._count("/api/job-matches/ranked/")
        self.assertEqual(len(payload["job_matches"]), 3)
        self.assertEqual(len(payload["organization_matches"]), 1)
        # Without evidence the requirement falls back to the profile field.
        self.assertEqual(
            payload["organization_matches"][0]["requirements"][0]["evidence_status"]["state"],
            "profile",
        )

        row = self._evidence(FieldKey.RTO_POLICY, "Remote", age_days=400)
        count, payload = self._count("/api/job-matches/ranked/")

        self.assertEqual(count, baseline + 1)
        with self.assertNumQueries(baseline + 1):
            self.client.get("/api/job-matches/ranked/")
        requirements = [
            m["requirements"][0]
            for m in payload["job_matches"] + payload["organization_matches"]
        ]
        self.assertEqual(len(requirements), 4)
        for requirement in requirements:
            self.assertEqual(requirement["source_id"], row.pk)
            # A stale accepted fact still evaluates as a match (#548), so the
            # response must say it is stale rather than leave it looking verified.
            self.assertEqual(requirement["status"], "match")
            self.assertEqual(
                requirement["evidence_status"],
                {
                    "state": "stale",
                    "last_verified_at": row.last_verified_at.isoformat(),
                    "source_domain": "acme.example",
                },
            )

    def test_assistant_match_tool_carries_the_same_status(self):
        from crank.agents.job_search.tools import get_matches_for_user

        CompanyFieldEvidence.objects.all().delete()
        row = self._evidence(FieldKey.RTO_POLICY, "Remote", age_days=400)
        with CaptureQueriesContext(connection) as captured:
            matches = get_matches_for_user(self.owner)
        evidence_queries = [
            q for q in captured.captured_queries
            if "crank_companyfieldevidence" in q["sql"] and '"id" IN' in q["sql"]
        ]
        self.assertEqual(len(evidence_queries), 1)
        ranked = self.client.get("/api/job-matches/ranked/").json()
        self.assertEqual(
            [m["requirements"] for m in matches["job_matches"]],
            [m["requirements"] for m in ranked["job_matches"]],
        )
        self.assertEqual(
            matches["organization_matches"][0]["requirements"],
            ranked["organization_matches"][0]["requirements"],
        )
        self.assertEqual(
            matches["job_matches"][0]["requirements"][0]["evidence_status"]["state"], "stale"
        )
        self.assertEqual(matches["job_matches"][0]["requirements"][0]["source_id"], row.pk)

    def _ranked_states(self, path):
        payload = self.client.get("/api/job-matches/ranked/").json()
        return {
            kind: [
                (r["status"], r["observed"], r["evidence_status"]["state"])
                for match in payload[kind]
                for r in match["requirements"]
                if r["path"] == path
            ]
            for kind in ("job_matches", "organization_matches")
        }

    def test_hybrid_evidence_does_not_verify_the_in_office_day_chip(self):
        CompanyFieldEvidence.objects.all().delete()
        self._evidence(FieldKey.RTO_POLICY, "Hybrid", age_days=2)
        UserPreference.objects.filter(user=self.owner).update(
            preferences={"work_location": {"max_in_office_days": 3, "modes": ["hybrid"]}}
        )

        days = self._ranked_states("work_location.max_in_office_days")
        # The evidence says "Hybrid"; the 3 is matching's reading of the word.
        self.assertEqual(days["job_matches"], [("match", 3, "sourced")] * 3)
        self.assertEqual(days["organization_matches"], [("match", 3, "sourced")])
        # The work mode is what the same fact does state.
        modes = self._ranked_states("work_location.modes")
        self.assertEqual(modes["job_matches"], [("match", "hybrid", "verified")] * 3)
        self.assertEqual(modes["organization_matches"], [("match", "hybrid", "verified")])

    def test_scoped_evidence_is_not_verified_on_a_job_chip(self):
        CompanyFieldEvidence.objects.all().delete()
        row = self._evidence(FieldKey.RTO_POLICY, "Remote", age_days=2)
        CompanyFieldEvidence.objects.filter(pk=row.pk).update(
            scope_json={"countries": ["Germany"]}
        )
        JobListing.all_objects.filter(pk=self.listings[0].pk).update(
            location_text="Berlin, Germany"
        )

        payload = self.client.get("/api/job-matches/ranked/").json()
        in_scope = next(
            m for m in payload["job_matches"] if m["listing_id"] == self.listings[0].pk
        )["requirements"][0]
        self.assertEqual(
            (in_scope["status"], in_scope["scope_ok"], in_scope["source_id"]),
            ("match", True, row.pk),
        )
        self.assertEqual(in_scope["evidence_status"]["state"], "sourced")
        for match in payload["job_matches"] + payload["organization_matches"]:
            for requirement in match["requirements"]:
                self.assertNotEqual(requirement["evidence_status"]["state"], "verified")

    def test_injected_match_service_path_is_annotated_too(self):
        """The this-search-only confirm response uses the injected path."""
        from crank.agents.job_search.tools import get_matches_for_user
        from crank.services import job_matching

        CompanyFieldEvidence.objects.all().delete()
        self._evidence(FieldKey.RTO_POLICY, "Remote first", age_days=2)

        def service(user, limit):
            return (
                job_matching.match_jobs(user, limit=limit),
                iter(job_matching.match_organizations(user, limit=limit)),
            )

        with CaptureQueriesContext(connection) as captured:
            matches = get_matches_for_user(self.owner, match_service=service)
        self.assertEqual(
            len([q for q in captured.captured_queries
                 if "crank_companyfieldevidence" in q["sql"] and '"id" IN' in q["sql"]]),
            1,
        )
        rows = matches["job_matches"] + matches["organization_matches"]
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["requirements"][0]["status"], "match")
            # Prose matching interpreted: sourced, which the chip says.
            self.assertEqual(row["requirements"][0]["evidence_status"]["state"], "sourced")
            self.assertEqual(row["reasons"], ["Remote"])

        # The same path withholds the reason once the fact is stale.
        CompanyFieldEvidence.objects.update(last_verified_at=self.now - timedelta(days=400))
        for row in get_matches_for_user(self.owner, match_service=service)["job_matches"]:
            self.assertEqual(row["requirements"][0]["evidence_status"]["state"], "stale")
            self.assertEqual(row["reasons"], [])
