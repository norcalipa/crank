# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Hidden employers never reach the assistant or the job-match surfaces (issue #536)."""

import itertools
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from crank.agents.job_search import page_context, tools
from crank.agents.job_search.errors import InvalidJobListingReferenceError
from crank.agents.job_search.service import JobSearchOrchestrator
from crank.agents.jobs import employer
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.organization import Organization
from crank.models.preference import UserPreference
from crank.services import job_matching, match_results
from crank.services.match_recompute import recompute_user
from crank.services.organization_visibility import (
    is_visible_organization,
    visible_employer_q,
    visible_organizations,
)
from crank.views import job_matches

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class VisibilityHelperTests(TestCase):
    def test_rule_and_parity_with_employer_resolution(self):
        for index, (status, public) in enumerate(itertools.product((0, 1), (False, True))):
            org = Organization.objects.create(name=f"Org {index}", status=status, public=public)
            expected = status == 1 and public is True
            with self.subTest(status=status, public=public):
                self.assertIs(is_visible_organization(org), expected)
                resolved = employer._resolve_candidates([org], "name")
                self.assertIs(resolved.organization is not None, expected)
                self.assertIs(visible_organizations().filter(pk=org.pk).exists(), expected)

    def test_none_is_not_visible(self):
        self.assertFalse(is_visible_organization(None))

    def test_visible_organizations_narrows_a_given_queryset(self):
        shown = Organization.objects.create(name="Shown")
        hidden = Organization.objects.create(name="Hidden", public=False)
        rows = visible_organizations(Organization.objects.filter(pk__in=[shown.pk, hidden.pk]))
        self.assertEqual(list(rows), [shown])

    def test_visible_employer_q_default_and_prefixed(self):
        source = JobSourceCatalog.objects.create(
            name="S", adapter_key="s.v1", base_url="https://jobs.example.test"
        )
        now = timezone.now()

        def listing(name, org):
            return JobListing.all_objects.create(
                source=source,
                external_id=name,
                canonical_url=f"https://jobs.example.test/{name}",
                employer_name=name,
                title=name,
                first_seen_at=now,
                last_seen_at=now,
                status=JobListing.Status.ACTIVE,
                organization=org,
            )

        visible = listing("visible", Organization.objects.create(name="V"))
        listing("private", Organization.objects.create(name="P", public=False))
        listing("inactive", Organization.objects.create(name="I", status=0))
        orphan = listing("orphan", None)
        titles = set(
            JobListing.objects.filter(visible_employer_q()).values_list("title", flat=True)
        )
        self.assertEqual(titles, {"visible", "orphan"})
        user = User.objects.create_user("u", password="x")
        match = JobMatch.objects.create(
            user=user,
            listing=orphan,
            organization=None,
            preference_version=1,
            ranker_version="1",
            score=1,
            first_matched_at=now,
            last_matched_at=now,
        )
        self.assertEqual(
            list(JobMatch.objects.filter(visible_employer_q("listing__"))), [match]
        )
        self.assertTrue(visible.pk)
        self.assertEqual(
            list(JobListing.objects.filter(visible_employer_q(), pk=visible.pk)), [visible]
        )


@override_settings(CACHES=LOCMEM)
class HiddenEmployerFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", password="secret")
        self.visible_org = Organization.objects.create(name="Visible Co")
        self.private_org = Organization.objects.create(name="Private Co", public=False)
        self.inactive_org = Organization.objects.create(name="Inactive Co", status=0)
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
            last_crawl_at=timezone.now(),
        )
        self.visible = self.make_listing("visible", self.visible_org)
        self.private = self.make_listing("private", self.private_org)
        self.inactive = self.make_listing("inactive", self.inactive_org)
        self.orphan = self.make_listing("orphan", None)

    def make_listing(self, name, org):
        now = timezone.now()
        return JobListing.all_objects.create(
            source=self.source,
            external_id=name,
            canonical_url=f"https://jobs.example.test/{name}",
            employer_name=org.name if org else name,
            title=name,
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=org,
        )

    def make_match(self, listing, organization="listing"):
        now = timezone.now()
        return JobMatch.objects.create(
            user=self.user,
            listing=listing,
            organization=listing.organization if organization == "listing" else organization,
            preference_version=1,
            ranker_version="1.0.0",
            score=50,
            first_matched_at=now,
            last_matched_at=now,
        )


class ListingToolTests(HiddenEmployerFixture):
    def test_listing_tool_returns_visible_and_orphan_only(self):
        rows = tools.search_job_listings({})
        self.assertEqual({r["id"] for r in rows}, {self.visible.pk, self.orphan.pk})
        for org in (self.private_org, self.inactive_org):
            self.assertEqual(
                tools.search_job_listings({"organization_id": org.pk}), []
            )

    def test_detail_tool_hides_hidden_employers(self):
        self.assertEqual(tools.get_job_listing_detail(self.visible.pk)["id"], self.visible.pk)
        self.assertEqual(tools.get_job_listing_detail(self.orphan.pk)["id"], self.orphan.pk)
        self.assertIsNone(tools.get_job_listing_detail(self.private.pk))
        self.assertIsNone(tools.get_job_listing_detail(self.inactive.pk))

    def test_hidden_listing_is_not_citable(self):
        rows = tools.search_job_listings({})
        known = tools.union_server_controlled_listing_ids(rows)
        self.assertNotIn(self.private.pk, known)
        self.assertNotIn(self.inactive.pk, known)
        JobSearchOrchestrator._validate_listing_citations((self.visible.pk,), known)
        with self.assertRaises(InvalidJobListingReferenceError):
            JobSearchOrchestrator._validate_listing_citations((self.private.pk,), known)

    def test_catalog_and_page_context_use_the_same_rule(self):
        names = {r["name"] for r in tools.normalize_organization_rows(
            tools.default_organization_datasource({}, 10))}
        self.assertEqual(names, {"Visible Co"})
        ids = [self.visible_org.pk, self.private_org.pk, self.inactive_org.pk]
        self.assertEqual(
            [r["id"] for r in page_context._load_organizations(ids)], [self.visible_org.pk]
        )
        self.assertIsNone(page_context._load_listing(self.private.pk))
        self.assertIsNotNone(page_context._load_listing(self.visible.pk))

    def test_load_listing_rechecks_the_employer_of_a_row_it_is_handed(self):
        row = JobListing.all_objects.select_related("organization").get(pk=self.private.pk)
        with mock.patch.object(
            tools, "default_job_listing_detail_datasource", return_value=row
        ):
            self.assertIsNone(page_context._load_listing(row.pk))

    def test_visibility_is_read_at_request_time(self):
        self.assertIsNone(tools.get_job_listing_detail(self.private.pk))
        Organization.objects.filter(pk=self.private_org.pk).update(public=True)
        self.assertIsNotNone(tools.get_job_listing_detail(self.private.pk))


class LiveMatchingTests(HiddenEmployerFixture):
    def setUp(self):
        super().setUp()
        UserPreference.objects.create(
            user=self.user, revision=0, preferences={"work_location": {"modes": ["remote"]}}
        )

    def test_match_jobs_and_relaxation_preview_exclude_hidden_employers(self):
        seen = []

        def capture(listings, *args, **kwargs):
            seen.append({listing.pk for listing in listings})
            return []

        with mock.patch.object(job_matching, "rank_listings_with_reasons", capture):
            job_matching.match_jobs(self.user)
            job_matching.relaxation_preview(self.user)
        self.assertGreaterEqual(len(seen), 2)
        for ids in seen:
            self.assertEqual(ids, {self.visible.pk, self.orphan.pk})

    def test_match_organizations_default_queryset_is_visible_only(self):
        captured = []
        with mock.patch.object(
            job_matching, "_resolve_evidence", side_effect=lambda ids: captured.append(set(ids)) or {}
        ):
            job_matching.match_organizations(self.user)
        self.assertEqual(captured, [{self.visible_org.pk}])

    def test_hide_recompute_unhide_restores_committed_rows_with_no_write(self):
        recompute_user(self.user, reason="preference")
        before = match_results.current_match_count(self.user)
        self.assertGreaterEqual(before, 1)
        # Hide the employer, then a recompute runs while it is hidden.
        Organization.objects.filter(pk=self.visible_org.pk).update(public=False)
        recompute_user(self.user, reason="preference")
        self.assertEqual(
            match_results.current_generation_queryset(self.user)
            .filter(listing=self.visible)
            .count(),
            0,
        )
        generation = MatchResultState.objects.get(user=self.user).current_generation
        writes = JobMatch.objects.filter(user=self.user).count()
        # Visible again: the committed rows come back with no further write.
        Organization.objects.filter(pk=self.visible_org.pk).update(public=True)
        self.assertEqual(match_results.current_match_count(self.user), before)
        self.assertEqual(
            MatchResultState.objects.get(user=self.user).current_generation, generation
        )
        self.assertEqual(JobMatch.objects.filter(user=self.user).count(), writes)


class MatchReadTests(HiddenEmployerFixture):
    def setUp(self):
        super().setUp()
        UserPreference.objects.create(
            user=self.user, revision=0, preferences={"work_location": {"modes": ["remote"]}}
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.m_visible = self.make_match(self.visible)
        self.m_orphan = self.make_match(self.orphan)
        self.m_private = self.make_match(self.private)
        self.m_inactive = self.make_match(self.inactive)
        # Stored organization hidden although the listing's is visible.
        self.other = self.make_listing("other", self.visible_org)
        self.m_stored = self.make_match(self.other, organization=self.private_org)
        # Stored organization visible, but the listing was re-linked to a hidden one.
        self.relinked = self.make_listing("relinked", self.visible_org)
        self.m_relinked = self.make_match(self.relinked)
        JobListing.all_objects.filter(pk=self.relinked.pk).update(
            organization=self.private_org
        )

    def shown(self):
        return {m.pk for m in JobMatch.objects.filter(match_results.presentable_match_q())}

    def test_presentable_match_q(self):
        self.assertEqual(self.shown(), {self.m_visible.pk, self.m_orphan.pk})

    def test_list_status_and_ranked_exclude_hidden_employers(self):
        payload = self.client.get("/api/job-matches/").json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            {r["id"] for r in payload["results"]}, {self.m_visible.pk, self.m_orphan.pk}
        )
        status = self.client.get("/api/job-matches/status/").json()
        self.assertEqual(status["state"], "ok")
        self.assertEqual(job_matches._status_count(self.user), (2, None))
        ranked = self.client.get("/api/job-matches/ranked/").json()
        names = {m.get("organization_name") for m in ranked["job_matches"]}
        self.assertNotIn("Private Co", names)
        self.assertNotIn("Inactive Co", names)

    def test_detail_seen_dismiss_answer_404_and_write_nothing(self):
        for match in (self.m_private, self.m_inactive, self.m_stored, self.m_relinked):
            with self.subTest(match=match.pk):
                self.assertEqual(self.client.get(f"/api/job-matches/{match.pk}/").status_code, 404)
                for action in ("seen", "dismiss"):
                    self.assertEqual(
                        self.client.post(f"/api/job-matches/{match.pk}/{action}/").status_code,
                        404,
                    )
                match.refresh_from_db()
                self.assertIsNone(match.seen_at)
                self.assertFalse(match.dismissed)
        self.assertEqual(
            self.client.get(f"/api/job-matches/{self.m_visible.pk}/").status_code, 200
        )

    def test_committed_generation_reads_and_counts_exclude_hidden_employers(self):
        recompute_user(self.user, reason="preference")
        published = match_results.current_match_count(self.user)
        self.assertGreaterEqual(published, 1)
        generation_ids = set(
            match_results.current_generation_queryset(self.user).values_list("listing_id", flat=True)
        )
        self.assertFalse(generation_ids & {self.private.pk, self.inactive.pk})
        # Hide the employer after the generation was published.
        Organization.objects.filter(pk=self.visible_org.pk).update(public=False)
        hidden_ids = set(
            match_results.current_generation_queryset(self.user).values_list("listing_id", flat=True)
        )
        self.assertNotIn(self.visible.pk, hidden_ids)
        self.assertEqual(
            [res.listing.pk for res in match_results.current_job_results(self.user, 10)
             if res.listing.pk == self.visible.pk],
            [],
        )
        with override_settings(MATCH_RESULTS_READ_ENABLED=True):
            ranked = self.client.get("/api/job-matches/ranked/").json()
            self.assertNotIn(
                "Visible Co", {m.get("organization_name") for m in ranked["job_matches"]}
            )
            count, generation = job_matches._status_count(self.user)
            self.assertIsNotNone(generation)
            self.assertEqual(count, match_results.current_match_count(self.user))
        # Visible again: the rows come back with no write.
        Organization.objects.filter(pk=self.visible_org.pk).update(public=True)
        self.assertEqual(match_results.current_match_count(self.user), published)

    def test_empty_state_match_count_excludes_hidden_employers(self):
        from crank import empty_state

        self.assertEqual(empty_state._match_count(self.user), 2)
