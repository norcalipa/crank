# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The company view's two session reads (issue #486)."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crank.models.company_profile import CompanyFieldEvidence
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.organization import Organization
from crank.models.preference import UserPreference, default_preferences
from crank.services.scores import (
    organization_api_cache_key,
    organization_provenance_api_cache_key,
    organization_scores_api_cache_key,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def prefs(**sections):
    document = default_preferences()
    for name, values in sections.items():
        if isinstance(values, dict) and isinstance(document.get(name), dict):
            document[name].update(values)
        else:
            document[name] = values
    return document


def evidence(organization, field_key, value, scope=None):
    now = timezone.now()
    return CompanyFieldEvidence.objects.create(
        organization=organization,
        field_key=field_key,
        value_text=value,
        source_url="https://example.test/evidence",
        source_domain="example.test",
        observed_at=now,
        last_verified_at=now,
        validation_version="1",
        extractor_version="1",
        state=CompanyFieldEvidence.State.ACCEPTED,
        scope_json=scope or {},
    )


@override_settings(CACHES=LOCMEM)
class CompanyViewTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("reader", password="secret")
        self.other = User.objects.create_user("bystander", password="secret")
        self.org = Organization.objects.create(
            name="Acme", rto_policy="H", funding_round="A", status=1, public=True
        )
        self.client.force_login(self.user)

    def url(self, name, pk=None):
        return reverse(name, args=[pk if pk is not None else self.org.pk])


class FitEndpointTests(CompanyViewTestBase):
    def fit(self, pk=None):
        return self.client.get(self.url("organization-fit", pk))

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        response = self.fit()
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_unknown_and_inactive_get_the_same_404(self):
        inactive = Organization.objects.create(name="Gone", status=0)
        unknown = self.fit(999999)
        gone = self.fit(inactive.pk)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(gone.status_code, 404)
        self.assertEqual(unknown.content, gone.content)
        self.assertEqual(unknown["Cache-Control"], "private, no-store")

    def test_post_is_refused(self):
        response = self.client.post(self.url("organization-fit"))
        self.assertEqual(response.status_code, 405)

    def test_no_preferences_row(self):
        body = self.fit().json()
        self.assertFalse(body["has_preferences"])
        self.assertEqual(body["requirements"], [])
        self.assertIsNone(body["fit_score"])
        self.assertEqual(body["organization_id"], self.org.pk)

    def test_default_preferences_have_no_requirements(self):
        UserPreference.objects.create(user=self.user, preferences=default_preferences(), schema_version=3)
        body = self.fit().json()
        self.assertFalse(body["has_preferences"])
        self.assertEqual(body["requirements"], [])

    def test_cache_control_is_private_no_store(self):
        self.assertEqual(self.fit()["Cache-Control"], "private, no-store")

    def test_hard_and_soft_requirements_are_split(self):
        UserPreference.objects.create(
            user=self.user,
            preferences=prefs(
                funding_stage=["A"],
                work_location={"max_in_office_days": 3},
                importance={"funding_stage": 0.4},
            ),
            schema_version=3,
        )
        body = self.fit().json()
        self.assertTrue(body["has_preferences"])
        by_path = {r["path"]: r for r in body["requirements"]}
        self.assertTrue(by_path["work_location.max_in_office_days"]["hard"])
        self.assertFalse(by_path["funding_stage"]["hard"])
        self.assertEqual(by_path["funding_stage"]["status"], "match")
        self.assertIn("evidence_status", by_path["funding_stage"])
        self.assertIsNone(by_path["funding_stage"]["scope"])
        self.assertEqual(
            set(body["revision"]), {"preference_revision", "ranking_version", "data_revision", "generated_at", "stale"}
        )
        self.assertIsNotNone(body["fit_score"])
        self.assertIn("coverage", body)
        self.assertIn("company_score", body)

    def test_the_observed_values_the_engine_sends_under_six_priorities(self):
        # The client turns these into words in static/js/company/FitSection.tsx
        # (`observedText`). If the engine's vocabulary changes, this fails first:
        # update that formatter and its table in FitSection.test.tsx with it.
        self.org.accelerated_vesting = True
        self.org.save()
        evidence(self.org, "funding_round", "Series B")
        evidence(self.org, "rto_policy", "Remote")
        evidence(self.org, "public_status", "Private company")
        UserPreference.objects.create(
            user=self.user,
            preferences=prefs(
                work_location={"modes": ["remote"], "max_in_office_days": 2},
                funding_stage=["public"],
                compensation={"require_public_company": True},
                industry=["Software"],
                vesting={"prefer_accelerated": True},
            ),
            schema_version=3,
        )
        by_path = {r["path"]: r["observed"] for r in self.fit().json()["requirements"]}
        self.assertEqual(
            by_path,
            {
                "work_location.modes": "remote",
                "work_location.max_in_office_days": 0,
                "funding_stage": "b",
                "compensation.require_public_company": "Private company",
                # An organization carries no industry of its own: nothing to show.
                "industry": None,
                "vesting.prefer_accelerated": True,
            },
        )

    def test_the_observed_values_the_engine_sends_for_every_other_path(self):
        self.maxDiff = None
        # Pins the shapes behind the table in FitSection.test.tsx (`observedText`).
        # The only other value a company can carry today is the reader's own
        # lower-case country, matched in accepted "locations" evidence. An
        # organization has no industry, culture, equity, cliff or vesting length of
        # its own, and the three listing-level paths (pay, region, remote flag)
        # cannot be decided without a listing, so those carry no value and no
        # source. When the model gains one of these fields this test fails: add
        # its shape to the table in FitSection.test.tsx and to `observedText`.
        evidence(self.org, "locations", "San Francisco, US")
        UserPreference.objects.create(
            user=self.user,
            preferences=prefs(
                compensation={"minimum_salary": 100000, "equity_minimum_percent": 0.25},
                work_location={"countries": ["US"]},
                geography={"regions": ["EMEA"], "remote_friendly": True},
                industry=["fin tech"],
                culture=["collaborative"],
                vesting={"max_cliff_months": 12, "max_vesting_months": 48},
            ),
            schema_version=3,
        )
        rows = {r["path"]: r for r in self.fit().json()["requirements"]}
        self.assertEqual(
            {path: row["observed"] for path, row in rows.items()},
            {
                "compensation.minimum_salary": None,
                "compensation.equity_minimum_percent": None,
                "work_location.countries": "us",
                "geography.regions": None,
                "geography.remote_friendly": None,
                "industry": None,
                "culture": None,
                "vesting.max_cliff_months": None,
                "vesting.max_vesting_months": None,
            },
        )
        for path in ("compensation.minimum_salary", "geography.regions", "geography.remote_friendly"):
            self.assertEqual(rows[path]["status"], "unknown")
            self.assertFalse(rows[path].get("evidence_status"))
            self.assertFalse(rows[path].get("source_kind"))

    def test_priorities_the_engine_cannot_check_are_sent_by_name(self):
        document = prefs(work_location={"modes": ["remote"]})
        document["roles"] = {"titles": ["Engineer"], "families": [], "seniority": []}
        document["compensation"]["minimum_total_compensation"] = 150000
        UserPreference.objects.create(user=self.user, preferences=document, schema_version=3)
        body = self.fit().json()
        self.assertTrue(body["has_preferences"])
        self.assertEqual(sorted(body["unsupported"]), ["compensation.minimum_total_compensation", "roles.titles"])
        self.client.logout()
        other = User.objects.create_user("only-unsupported", password="secret")
        only = prefs()
        only["roles"] = {"titles": ["Engineer"], "families": [], "seniority": []}
        UserPreference.objects.create(user=other, preferences=only, schema_version=3)
        self.client.force_login(other)
        body = self.fit().json()
        self.assertFalse(body["has_preferences"])
        self.assertEqual(body["unsupported"], ["roles.titles"])

    def test_an_excluded_company_still_explains_itself(self):
        evidence(self.org, "funding_round", "Series A")
        evidence(self.org, "rto_policy", "Hybrid")
        UserPreference.objects.create(
            user=self.user,
            preferences=prefs(
                funding_stage=["C"],
                work_location={"max_in_office_days": 0},
                importance={"funding_stage": 1.0},
            ),
            schema_version=3,
        )
        body = self.fit().json()
        statuses = {r["path"]: (r["status"], r["hard"]) for r in body["requirements"]}
        self.assertEqual(statuses["funding_stage"], ("mismatch", True))
        self.assertEqual(statuses["work_location.max_in_office_days"], ("mismatch", True))

    def test_a_fact_that_does_not_cover_the_reader_is_never_a_match(self):
        evidence(self.org, "funding_round", "Series A", scope={"countries": ["Narnia"]})
        UserPreference.objects.create(
            user=self.user, preferences=prefs(funding_stage=["A"]), schema_version=3
        )
        row = self.fit().json()["requirements"][0]
        self.assertFalse(row["scope_ok"])
        self.assertNotEqual(row["status"], "match")
        self.assertEqual(row["scope"], {"countries": ["Narnia"]})

    def test_an_out_of_scope_match_from_the_engine_is_downgraded(self):
        from crank.agents.jobs import matching

        real = matching.evaluate_requirements

        def leaky(*args, **kwargs):
            return [
                matching.RequirementOutcome(o.path, "match", o.observed, o.source_kind, o.source_id, scope_ok=False)
                for o in real(*args, **kwargs)
            ]

        UserPreference.objects.create(
            user=self.user, preferences=prefs(funding_stage=["A"]), schema_version=3
        )
        with mock.patch("crank.services.job_matching.evaluate_requirements", leaky):
            row = self.fit().json()["requirements"][0]
        self.assertEqual(row["status"], "unknown")

    def test_a_company_wide_and_a_scoped_row_report_their_scope(self):
        evidence(self.org, "rto_policy", "Hybrid")
        evidence(self.org, "funding_round", "Series A", scope={"role_families": ["Engineering"], "other": ["x"]})
        UserPreference.objects.create(
            user=self.user,
            preferences=prefs(funding_stage=["A"], work_location={"max_in_office_days": 3}),
            schema_version=3,
        )
        by_path = {r["path"]: r for r in self.fit().json()["requirements"]}
        self.assertIsNone(by_path["work_location.max_in_office_days"]["scope"])
        self.assertEqual(by_path["funding_stage"]["scope"], {"role_families": ["Engineering"]})

    def test_an_evidence_row_with_a_junk_scope_reads_company_wide(self):
        from crank.services.job_matching import _scope_of

        row = mock.Mock(scope_json=["not", "a", "mapping"])
        self.assertIsNone(_scope_of(row))
        self.assertIsNone(_scope_of(mock.Mock(scope_json={"countries": [], "x": ["y"]})))
        self.assertEqual(_scope_of(mock.Mock(scope_json={"countries": "US"})), {"countries": ["US"]})

    def test_one_readers_priorities_never_shape_anothers_answer(self):
        UserPreference.objects.create(
            user=self.user, preferences=prefs(funding_stage=["A"]), schema_version=3
        )
        UserPreference.objects.create(
            user=self.other, preferences=prefs(funding_stage=["C"]), schema_version=3
        )
        mine = self.fit().json()["requirements"][0]["status"]
        self.client.force_login(self.other)
        theirs = self.fit().json()["requirements"][0]["status"]
        self.assertEqual((mine, theirs), ("match", "mismatch"))
        UserPreference.objects.filter(user=self.other).delete()
        self.assertFalse(self.fit().json()["has_preferences"])

    def test_a_bounded_number_of_queries(self):
        evidence(self.org, "rto_policy", "Hybrid")
        UserPreference.objects.create(
            user=self.user,
            preferences=prefs(funding_stage=["A"], work_location={"max_in_office_days": 3}),
            schema_version=3,
        )
        self.fit()  # warm the session and the average-score cache
        with self.assertNumQueries(8):
            self.assertEqual(self.fit().status_code, 200)

    def test_nothing_is_written_to_the_per_organization_cache_keys(self):
        UserPreference.objects.create(
            user=self.user, preferences=prefs(funding_stage=["A"]), schema_version=3
        )
        keys = [
            organization_api_cache_key(self.org.pk),
            organization_provenance_api_cache_key(self.org.pk),
            organization_scores_api_cache_key(self.org.pk),
        ]
        self.fit()
        self.assertEqual([cache.get(k) for k in keys], [None, None, None])


class JobsEndpointTests(CompanyViewTestBase):
    def setUp(self):
        super().setUp()
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic", adapter_key="synthetic.v1", base_url="https://jobs.example.test", enabled=True
        )

    def jobs(self, pk=None):
        return self.client.get(self.url("organization-jobs", pk))

    def listing(self, n, status=JobListing.Status.ACTIVE, organization=None, **values):
        now = timezone.now()
        return JobListing.all_objects.create(
            source=self.source,
            external_id=f"job-{n}",
            canonical_url=f"https://jobs.example.test/{n}",
            employer_name="Acme",
            title=f"Role {n}",
            first_seen_at=now - timedelta(days=2),
            last_seen_at=now - timedelta(minutes=n),
            status=status,
            organization=organization or self.org,
            description_excerpt="private excerpt",
            **values,
        )

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        response = self.jobs()
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_unknown_and_inactive_get_the_same_404(self):
        inactive = Organization.objects.create(name="Gone", status=0)
        unknown, gone = self.jobs(999999), self.jobs(inactive.pk)
        self.assertEqual((unknown.status_code, gone.status_code), (404, 404))
        self.assertEqual(unknown.content, gone.content)

    def test_post_is_refused(self):
        self.assertEqual(self.client.post(self.url("organization-jobs")).status_code, 405)

    def test_only_active_listings_of_this_organization(self):
        elsewhere = Organization.objects.create(name="Elsewhere", status=1)
        self.listing(1)
        self.listing(2, status=JobListing.Status.CLOSED)
        self.listing(3, status=JobListing.Status.EXPIRED)
        self.listing(4, organization=elsewhere)
        response = self.jobs()
        body = response.json()
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(body["inventory"], "available")
        self.assertEqual(body["count"], 1)
        self.assertEqual([r["title"] for r in body["results"]], ["Role 1"])
        row = body["results"][0]
        self.assertNotIn("description_excerpt", row)
        self.assertNotIn("organization", row)
        self.assertEqual(row["canonical_url"], "https://jobs.example.test/1")
        for key in ("id", "title", "location_text", "is_remote", "compensation_min", "compensation_max",
                    "compensation_currency", "compensation_interval", "status"):
            self.assertIn(key, row)

    def test_capped_at_25(self):
        for n in range(1, 31):
            self.listing(n)
        body = self.jobs().json()
        self.assertEqual(body["count"], 25)
        self.assertEqual(len(body["results"]), 25)

    def test_no_enabled_source_is_unavailable_not_empty(self):
        self.listing(1)
        JobSourceCatalog.objects.update(enabled=False)
        unavailable = self.jobs().json()
        self.assertEqual(unavailable["inventory"], "unavailable")
        self.assertEqual(unavailable["results"], [])
        JobSourceCatalog.objects.update(enabled=True)
        JobListing.all_objects.all().delete()
        empty = self.jobs().json()
        self.assertEqual((empty["inventory"], empty["count"], empty["results"]), ("available", 0, []))

    def test_a_bounded_number_of_queries(self):
        for n in range(1, 6):
            self.listing(n)
        self.jobs()
        with self.assertNumQueries(5):
            self.assertEqual(self.jobs().status_code, 200)

    def test_nothing_is_written_to_the_per_organization_cache_keys(self):
        self.listing(1)
        keys = [
            organization_api_cache_key(self.org.pk),
            organization_provenance_api_cache_key(self.org.pk),
            organization_scores_api_cache_key(self.org.pk),
        ]
        self.jobs()
        self.assertEqual([cache.get(k) for k in keys], [None, None, None])
