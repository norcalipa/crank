# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The company view's two session reads (issue #486)."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache, caches
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crank.models.company_profile import CompanyFieldEvidence
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.organization import Organization
from crank.models.preference import UserPreference, default_preferences
from crank.services.scores import (
    organization_api_cache_key,
    organization_avg_scores_cache_key,
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
        self.assertIsNone(body["priorities_met"])
        self.assertNotIn("fit_score", body)
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
        self.assertEqual(body["priorities_met"], {"met": 1, "counted": 2})
        self.assertNotIn("fit_score", body)
        self.assertEqual(
            {path: r["counted"] for path, r in by_path.items()},
            {"funding_stage": True, "work_location.max_in_office_days": True},
        )
        self.assertIn("coverage", body)
        self.assertIn("company_score", body)

    def test_priorities_met_counts_only_what_a_company_fact_can_decide(self):
        evidence(self.org, "funding_round", "Series A")
        evidence(self.org, "rto_policy", "Hybrid")
        UserPreference.objects.create(
            user=self.user,
            preferences=prefs(
                funding_stage=["A"],
                work_location={"modes": ["remote"]},
                compensation={"minimum_salary": 100000, "currency": "USD"},
                geography={"remote_friendly": True},
            ),
            schema_version=3,
        )
        body = self.fit().json()
        by_path = {r["path"]: r for r in body["requirements"]}
        self.assertEqual(body["priorities_met"], {"met": 1, "counted": 2})
        self.assertEqual(
            {path: r["counted"] for path, r in by_path.items()},
            {
                "funding_stage": True,
                "work_location.modes": True,
                "compensation.minimum_salary": False,
                "geography.remote_friendly": False,
            },
        )
        meets = [r for r in body["requirements"] if r["status"] == "match" and r["scope_ok"] and r["counted"]]
        self.assertEqual(len(meets), body["priorities_met"]["met"])

    def test_only_per_job_priorities_leave_the_figure_empty(self):
        UserPreference.objects.create(
            user=self.user,
            preferences=prefs(compensation={"minimum_salary": 100000, "currency": "USD"}),
            schema_version=3,
        )
        body = self.fit().json()
        self.assertTrue(body["requirements"])
        self.assertTrue(all(r["counted"] is False for r in body["requirements"]))
        self.assertIsNone(body["priorities_met"])

    def test_a_fact_that_does_not_cover_the_reader_is_counted_but_not_met(self):
        evidence(self.org, "funding_round", "Series A", scope={"countries": ["Narnia"]})
        UserPreference.objects.create(
            user=self.user, preferences=prefs(funding_stage=["A"]), schema_version=3
        )
        body = self.fit().json()
        self.assertEqual(body["priorities_met"], {"met": 0, "counted": 1})
        self.assertTrue(body["requirements"][0]["counted"])

    def test_an_excluded_company_still_gets_its_figure(self):
        evidence(self.org, "funding_round", "Series A")
        UserPreference.objects.create(
            user=self.user,
            preferences=prefs(funding_stage=["A"], exclusions={"companies": ["Acme"]}),
            schema_version=3,
        )
        body = self.fit().json()
        self.assertTrue(body["excluded"])
        self.assertEqual(body["priorities_met"], {"met": 1, "counted": 1})

    def test_a_reader_with_only_exclusions_has_no_figure(self):
        UserPreference.objects.create(
            user=self.user, preferences=prefs(exclusions={"companies": ["Acme"]}), schema_version=3
        )
        self.assertIsNone(self.fit().json()["priorities_met"])

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

    def test_the_only_shared_entry_it_fills_is_the_reader_neutral_average_scores(self):
        # `_company_score` reads the company's ratings through
        # `Organization.avg_scores()`, the one read path every reader of a
        # company's scores uses; it fills this entry
        # (`cache.get_or_set`). The value depends on the company's ratings
        # alone, never on the reader, so no private data reaches the cache.
        UserPreference.objects.create(
            user=self.user, preferences=prefs(funding_stage=["A"]), schema_version=3
        )
        key = organization_avg_scores_cache_key(self.org.pk)
        self.assertIsNone(cache.get(key))
        self.fit()
        mine = cache.get(key)
        self.assertEqual(mine, [])  # a company with no ratings
        cache.clear()
        UserPreference.objects.create(
            user=self.other, preferences=prefs(funding_stage=["C"], work_location={"modes": ["remote"]}),
            schema_version=3,
        )
        self.client.force_login(self.other)
        self.fit()
        self.assertEqual(cache.get(key), mine)

    def test_a_fit_and_a_jobs_read_fill_no_other_cache_entry(self):
        # The whole key set, so an answer stored for the reader under a new
        # per-company key (the fit answer, the jobs list) fails here too.
        UserPreference.objects.create(
            user=self.user, preferences=prefs(funding_stage=["A"]), schema_version=3
        )
        self.fit()
        self.client.get(self.url("organization-jobs"))
        written = {key.split(":", 2)[-1] for key in caches["default"]._cache}
        self.assertEqual(written, {organization_avg_scores_cache_key(self.org.pk)})

    def test_a_cold_read_costs_one_more_query_than_a_warm_one(self):
        UserPreference.objects.create(
            user=self.user, preferences=prefs(funding_stage=["A"]), schema_version=3
        )
        with self.assertNumQueries(8):
            self.fit()
        with self.assertNumQueries(7):
            self.fit()

    def test_a_reader_who_excluded_this_company_is_told(self):
        UserPreference.objects.create(
            user=self.user,
            preferences=prefs(work_location={"modes": ["remote"]}, exclusions={"companies": [" ACME "]}),
            schema_version=3,
        )
        body = self.fit().json()
        self.assertTrue(body["excluded"])
        self.assertTrue(body["has_preferences"])
        self.assertTrue(body["requirements"])  # still explains itself
        UserPreference.objects.filter(user=self.user).update(
            preferences=prefs(work_location={"modes": ["remote"]}, exclusions={"companies": ["Other Corp"]})
        )
        self.assertFalse(self.fit().json()["excluded"])

    def test_a_reader_whose_only_priority_is_an_exclusion_has_priorities(self):
        for exclusions in (
            {"companies": ["Acme"]},
            {"companies": ["Other Corp"]},
            {"titles": ["Manager"]},
            {"industries": ["Mining"]},
            {"locations": ["Mars"]},
        ):
            with self.subTest(exclusions=exclusions):
                UserPreference.objects.update_or_create(
                    user=self.user, defaults={"preferences": prefs(exclusions=exclusions), "schema_version": 3}
                )
                body = self.fit().json()
                self.assertTrue(body["has_preferences"])
                self.assertEqual(body["requirements"], [])
                # No requirement row, no share of rows decided: not 0%.
                self.assertIsNone(body["coverage"])
        UserPreference.objects.filter(user=self.user).update(preferences=prefs())
        body = self.fit().json()
        self.assertEqual((body["has_preferences"], body["excluded"]), (False, False))

    def test_an_exclusion_beside_only_unsupported_priorities_has_no_coverage_and_lists_what_is_unchecked(self):
        document = prefs(exclusions={"titles": ["Sales"]})
        document["roles"] = {"titles": ["Engineer"], "families": [], "seniority": []}
        UserPreference.objects.update_or_create(
            user=self.user, defaults={"preferences": document, "schema_version": 3}
        )
        body = self.fit().json()
        self.assertTrue(body["has_preferences"])
        self.assertEqual(body["requirements"], [])
        self.assertIsNone(body["coverage"])
        self.assertIn("roles.titles", body["unsupported"])

    def test_coverage_is_a_share_once_there_is_a_requirement_row(self):
        UserPreference.objects.update_or_create(
            user=self.user,
            defaults={"preferences": prefs(work_location={"modes": ["remote"]}, exclusions={"titles": ["Sales"]}), "schema_version": 3},
        )
        body = self.fit().json()
        self.assertTrue(body["requirements"])
        self.assertIsNotNone(body["coverage"])

    def test_a_company_that_is_not_public_still_gets_an_answer_until_561(self):
        # Which records get a page is the owner's open decision (#561): the
        # rule is the three organization endpoints' (active records only).
        Organization.objects.filter(pk=self.org.pk).update(public=False)
        self.assertEqual(self.fit().status_code, 200)

    def test_the_numbers_are_sent(self):
        Organization.objects.filter(pk=self.org.pk).update(funding_round="A")
        UserPreference.objects.create(
            user=self.user, preferences=prefs(funding_stage=["A"]), schema_version=3
        )
        # `coverage` is the share of decided requirements, `company_score` the
        # plain average of the company's rating types.
        with mock.patch.object(
            Organization, "avg_scores", return_value=[{"avg_score": 4.0}, {"avg_score": 2.0}]
        ):
            body = self.fit().json()
        self.assertEqual(body["coverage"], 1.0)
        self.assertEqual(body["company_score"], 3.0)
        self.assertEqual(body["priorities_met"], {"met": 1, "counted": 1})
        self.assertIsNone(self.fit().json()["company_score"])  # no ratings


class JobsEndpointTests(CompanyViewTestBase):
    def setUp(self):
        super().setUp()
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )

    def jobs(self, pk=None):
        return self.client.get(self.url("organization-jobs", pk))

    def listing(self, n, status=JobListing.Status.ACTIVE, organization=None, title_prefix="Role", **values):
        now = timezone.now()
        return JobListing.all_objects.create(
            source=self.source,
            external_id=f"job-{n}",
            canonical_url=f"https://jobs.example.test/{n}",
            employer_name="Acme",
            title=f"{title_prefix} {n}",
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

    def test_listings_of_blocked_pending_and_disabled_sources_never_appear(self):
        # The one rule every other reader of job inventory uses (`match_inventory`):
        # an approved AND enabled source. A block changes `approval_state` only,
        # so a blocked source keeps `enabled=True` and its listings stay active.
        Approval = JobSourceCatalog.ApprovalState
        sources = {"approved": self.source}
        for name, approval, enabled in (
            ("blocked", Approval.BLOCKED, True),
            ("pending", Approval.PENDING, True),
            ("disabled", Approval.APPROVED, False),
        ):
            sources[name] = JobSourceCatalog.objects.create(
                name=name, adapter_key=f"{name}.v1", base_url=f"https://jobs.example.test/{name}",
                approval_state=approval, enabled=enabled,
            )
        for n, name in enumerate(sources, start=1):
            self.source = sources[name]
            self.listing(n, title_prefix=name)
        body = self.jobs().json()
        self.assertEqual(body["inventory"], "available")
        self.assertEqual([r["title"] for r in body["results"]], ["approved 1"])
        # Only the blocked and the pending source are left: nothing is "available".
        JobSourceCatalog.objects.filter(pk__in=[sources["approved"].pk, sources["disabled"].pk]).update(enabled=False)
        gone = self.jobs().json()
        self.assertEqual((gone["inventory"], gone["count"], gone["results"]), ("unavailable", 0, []))
        # Every source enabled but none approved: still unavailable.
        JobSourceCatalog.objects.update(enabled=True, approval_state=Approval.PENDING)
        self.assertEqual(self.jobs().json()["inventory"], "unavailable")

    def test_the_unavailable_answer_is_not_cached_by_the_browser_either(self):
        JobSourceCatalog.objects.update(enabled=False)
        response = self.jobs()
        self.assertEqual(response.json()["inventory"], "unavailable")
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_a_company_that_is_not_public_still_gets_an_answer_until_561(self):
        Organization.objects.filter(pk=self.org.pk).update(public=False)
        self.assertEqual(self.jobs().json()["inventory"], "available")

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
