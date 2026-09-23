# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Unit tests for committed-generation reads (issue #475)."""

import unittest.mock
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.organization import Organization
from crank.models.preference import UserPreference
from crank.services import match_results
from crank.services.match_recompute import recompute_user


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ReadEnabledGateTests(TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(match_results.read_enabled())

    @override_settings(MATCH_RESULTS_READ_ENABLED=True)
    def test_enabled_flag_alone_is_enabled_absent_switch(self):
        self.assertTrue(match_results.read_enabled())

    @override_settings(MATCH_RESULTS_READ_ENABLED=True)
    def test_switch_off_blocks_even_with_flag_on(self):
        from crank.models.monitoring import CapabilitySwitch

        CapabilitySwitch.objects.create(key="match_results_read", enabled=False)
        self.assertFalse(match_results.read_enabled())


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class CurrentGenerationQuerysetTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", password="secret")
        self.organization = Organization.objects.create(name="Acme")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        now = timezone.now()
        self.listing = JobListing.all_objects.create(
            source=self.source,
            external_id="engineer",
            canonical_url="https://jobs.example.test/engineer",
            employer_name=self.organization.name,
            title="Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=self.organization,
        )

    def test_no_user_returns_empty(self):
        self.assertFalse(match_results.current_generation_queryset(None).exists())

    def test_user_with_no_pk_returns_empty(self):
        anon = User(username="anon")
        self.assertFalse(match_results.current_generation_queryset(anon).exists())

    def test_no_preferences_returns_empty(self):
        self.assertFalse(match_results.current_generation_queryset(self.user).exists())
        self.assertEqual(match_results.current_match_count(self.user), 0)

    def test_no_generation_yet_returns_empty(self):
        UserPreference.objects.create(
            user=self.user, revision=0, preferences={"work_location": {"modes": ["remote"]}}
        )
        self.assertFalse(match_results.current_generation_queryset(self.user).exists())

    def test_current_generation_excludes_dismissed_and_inactive(self):
        UserPreference.objects.create(
            user=self.user, revision=0, preferences={"work_location": {"modes": ["remote"]}}
        )
        recompute_user(self.user, reason="preference")
        self.assertEqual(match_results.current_match_count(self.user), 1)

        match = JobMatch.objects.get()
        match.dismissed = True
        match.save(update_fields=["dismissed", "modified"])
        self.assertEqual(match_results.current_match_count(self.user), 0)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class CurrentJobResultsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", password="secret")
        self.organization = Organization.objects.create(name="Acme")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        now = timezone.now()
        self.listing = JobListing.all_objects.create(
            source=self.source,
            external_id="engineer",
            canonical_url="https://jobs.example.test/engineer",
            employer_name=self.organization.name,
            title="Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=self.organization,
        )

    def test_no_state_returns_empty(self):
        self.assertEqual(match_results.current_job_results(self.user, 10), [])

    def test_state_without_generation_returns_empty(self):
        MatchResultState.objects.create(user=self.user)
        self.assertEqual(match_results.current_job_results(self.user, 10), [])

    def test_preference_exists_but_no_generation_yet_returns_empty(self):
        UserPreference.objects.create(user=self.user, revision=0)
        MatchResultState.objects.create(user=self.user)
        self.assertEqual(match_results.current_job_results(self.user, 10), [])

    def test_no_preference_row_returns_empty(self):
        state = MatchResultState.objects.create(user=self.user, current_generation=1)
        self.assertEqual(match_results.current_job_results(self.user, 10), [])

    def test_returns_rows_with_revision_block(self):
        UserPreference.objects.create(
            user=self.user, revision=0, preferences={"work_location": {"modes": ["remote"]}}
        )
        outcome = recompute_user(self.user, reason="preference")
        results = match_results.current_job_results(self.user, 10)
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row.listing_id, self.listing.pk)
        self.assertEqual(row.result_generation, outcome.generation)
        self.assertFalse(row.stale)
        self.assertFalse(row.pending)

    def test_stale_when_preference_revision_advances_after_publish(self):
        pref = UserPreference.objects.create(
            user=self.user, revision=0, preferences={"work_location": {"modes": ["remote"]}}
        )
        recompute_user(self.user, reason="preference")
        pref.revision = 1
        pref.save(update_fields=["revision", "modified"])
        results = match_results.current_job_results(self.user, 10)
        self.assertTrue(results[0].stale)

    @override_settings(MATCH_RECOMPUTE_ENABLED=True)
    def test_pending_true_when_stale_and_recompute_enabled(self):
        pref = UserPreference.objects.create(
            user=self.user, revision=0, preferences={"work_location": {"modes": ["remote"]}}
        )
        recompute_user(self.user, reason="preference")
        pref.revision = 1
        pref.save(update_fields=["revision", "modified"])
        results = match_results.current_job_results(self.user, 10)
        self.assertTrue(results[0].pending)

    def test_company_score_handles_missing_avg_scores(self):
        self.assertIsNone(match_results._company_score_of(None))
        self.assertIsNone(match_results._company_score_of(self.organization))


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class LoadCurrentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", password="secret")

    def test_no_preference_returns_none_state_and_empty_rows(self):
        state, rows = match_results.load_current(self.user)
        self.assertIsNone(state)
        self.assertEqual(rows, [])

    def test_preference_without_generation_returns_none_state(self):
        UserPreference.objects.create(user=self.user, revision=0)
        state, rows = match_results.load_current(self.user)
        self.assertIsNone(state)
        self.assertEqual(rows, [])

    def test_pins_rows_to_the_read_states_generation(self):
        UserPreference.objects.create(
            user=self.user, revision=0, preferences={"work_location": {"modes": ["remote"]}}
        )
        organization = Organization.objects.create(name="Acme")
        source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        now = timezone.now()
        JobListing.all_objects.create(
            source=source,
            external_id="engineer",
            canonical_url="https://jobs.example.test/engineer",
            employer_name=organization.name,
            title="Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=organization,
        )
        outcome = recompute_user(self.user, reason="preference")
        state, rows = match_results.load_current(self.user)
        self.assertEqual(state.current_generation, outcome.generation)
        self.assertEqual([row.result_generation for row in rows], [outcome.generation])

    def test_retries_when_generation_advances_between_state_read_and_materialize(self):
        """A publish that lands mid-read must not leave the caller with an
        empty or partial generation (issue #475 review round 2, MAJOR
        finding): ``load_current`` re-validates ``current_generation`` after
        materializing rows and retries against the new generation instead
        of returning a stale, now-empty snapshot."""
        UserPreference.objects.create(
            user=self.user, revision=0, preferences={"work_location": {"modes": ["remote"]}}
        )
        organization = Organization.objects.create(name="Acme")
        source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        now = timezone.now()
        JobListing.all_objects.create(
            source=source,
            external_id="engineer",
            canonical_url="https://jobs.example.test/engineer",
            employer_name=organization.name,
            title="Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=organization,
        )
        first_outcome = recompute_user(self.user, reason="preference")
        self.assertEqual(first_outcome.status.value, "published")

        real_queryset = match_results.current_generation_queryset
        call_count = {"n": 0}

        def _interleaving_queryset(user, *, generation=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Simulate a publish landing between load_current's state
                # read and its row materialization: bump the ranker
                # version so the state is stale, forcing a real second
                # generation before this (first) attempt's rows are
                # fetched.
                pref = UserPreference.objects.get(user=user)
                pref.revision += 1
                pref.save(update_fields=["revision", "modified"])
                second_outcome = recompute_user(
                    user, reason="preference", force=True
                )
                self.assertEqual(second_outcome.status.value, "published")
                self.assertGreater(second_outcome.generation, first_outcome.generation)
            return real_queryset(user, generation=generation)

        with unittest.mock.patch.object(
            match_results, "current_generation_queryset", side_effect=_interleaving_queryset
        ):
            state, rows = match_results.load_current(self.user)

        self.assertIsNotNone(state)
        self.assertGreater(state.current_generation, first_outcome.generation)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].result_generation, state.current_generation)

    def test_returns_best_effort_snapshot_when_retries_are_exhausted(self):
        """Under sustained concurrent publishing that never lets the read
        stabilize, ``load_current`` must give up after its bounded retry
        count and return the last snapshot it read, rather than looping
        forever (issue #475 review round 2, MINOR finding 1)."""
        UserPreference.objects.create(
            user=self.user, revision=0, preferences={"work_location": {"modes": ["remote"]}}
        )
        organization = Organization.objects.create(name="Acme")
        source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        now = timezone.now()
        JobListing.all_objects.create(
            source=source,
            external_id="engineer",
            canonical_url="https://jobs.example.test/engineer",
            employer_name=organization.name,
            title="Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=organization,
        )
        recompute_user(self.user, reason="preference")

        real_queryset = match_results.current_generation_queryset

        def _always_interleaving_queryset(user, *, generation=None):
            # Every attempt races a fresh publish in between reading the
            # generation and materializing rows, so the read can never
            # stabilize within the bounded retry count.
            pref = UserPreference.objects.get(user=user)
            pref.revision += 1
            pref.save(update_fields=["revision", "modified"])
            recompute_user(user, reason="preference", force=True)
            return real_queryset(user, generation=generation)

        with unittest.mock.patch.object(
            match_results,
            "current_generation_queryset",
            side_effect=_always_interleaving_queryset,
        ):
            state, rows = match_results.load_current(self.user)

        # A best-effort (state, rows) pair is still returned -- never an
        # infinite loop or an exception -- even though it may already be
        # superseded by the time the caller sees it.
        self.assertIsNotNone(state)
        self.assertIsInstance(rows, list)


class TagMismatchTests(TestCase):
    def test_none_current_value_is_never_a_mismatch(self):
        """There is nothing to compare against when the current value is
        unknown, so any stamped value (including a differing one) is not a
        mismatch on its own."""
        self.assertFalse(match_results._tag_mismatch(None, None))
        self.assertFalse(match_results._tag_mismatch(None, "1.0.0"))

    def test_missing_stamped_value_with_known_current_is_a_mismatch(self):
        self.assertTrue(match_results._tag_mismatch(3, None))

    def test_matching_values_are_not_a_mismatch(self):
        self.assertFalse(match_results._tag_mismatch(3, 3))

    def test_differing_values_are_a_mismatch(self):
        self.assertTrue(match_results._tag_mismatch(3, 4))


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class IsStaleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", password="secret")

    def test_none_state_or_pref_is_not_stale(self):
        pref = UserPreference.objects.create(user=self.user, revision=0)
        state = MatchResultState.objects.create(
            user=self.user, current_generation=1, preference_revision=0,
            preference_version=3, ranker_version="1.0.0",
        )
        self.assertFalse(match_results.is_stale(None, pref))
        self.assertFalse(match_results.is_stale(state, None))

    def test_schema_version_mismatch_is_stale(self):
        pref = UserPreference.objects.create(user=self.user, revision=0, schema_version=4)
        state = MatchResultState.objects.create(
            user=self.user, current_generation=1, preference_revision=0,
            preference_version=3, ranker_version="1.0.0",
        )
        self.assertTrue(match_results.is_stale(state, pref, ranker_version="1.0.0"))

    def test_ranker_version_mismatch_is_stale(self):
        pref = UserPreference.objects.create(user=self.user, revision=0, schema_version=3)
        state = MatchResultState.objects.create(
            user=self.user, current_generation=1, preference_revision=0,
            preference_version=3, ranker_version="1.0.0-old",
        )
        self.assertTrue(match_results.is_stale(state, pref, ranker_version="2.0.0"))

    def test_matching_tags_are_not_stale(self):
        pref = UserPreference.objects.create(user=self.user, revision=0, schema_version=3)
        state = MatchResultState.objects.create(
            user=self.user, current_generation=1, preference_revision=0,
            preference_version=3, ranker_version="1.0.0",
        )
        self.assertFalse(match_results.is_stale(state, pref, ranker_version="1.0.0"))

    def test_greater_stamped_preference_revision_is_stale(self):
        """A stamped revision *ahead* of the current preference (shouldn't
        happen, but previously read as current) is a mismatch too (issue
        #475 review round 2, MINOR finding 2)."""
        pref = UserPreference.objects.create(user=self.user, revision=0, schema_version=3)
        state = MatchResultState.objects.create(
            user=self.user, current_generation=1, preference_revision=1,
            preference_version=3, ranker_version="1.0.0",
        )
        self.assertTrue(match_results.is_stale(state, pref, ranker_version="1.0.0"))

    def test_missing_stamped_tags_are_stale_when_current_value_exists(self):
        """A generation with no stamped tags (e.g. pre-#475) must not read
        as current once the preference/ranker have real values (issue #475
        review round 2, MINOR finding 2)."""
        pref = UserPreference.objects.create(user=self.user, revision=0, schema_version=3)
        state = MatchResultState.objects.create(
            user=self.user, current_generation=1, preference_revision=None,
            preference_version=None, ranker_version="",
        )
        self.assertTrue(match_results.is_stale(state, pref, ranker_version="1.0.0"))


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class RevisionBlockTests(TestCase):
    def test_none_state_returns_empty_block(self):
        block = match_results.revision_block(None, stale=False)
        self.assertIsNone(block["result_generation"])
        self.assertIsNone(block["preference_revision"])
        self.assertEqual(block["ranking_version"], "")
        self.assertFalse(block["pending"])

    def test_state_with_no_generated_at(self):
        user = User.objects.create_user("owner", password="secret")
        state = MatchResultState.objects.create(user=user)
        block = match_results.revision_block(state, stale=False)
        self.assertIsNone(block["generated_at"])
