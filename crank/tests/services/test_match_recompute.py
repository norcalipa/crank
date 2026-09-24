# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the versioned, CAS-published job-match recompute (issue #475)."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from crank.agents.jobs.match_persist import (
    MatchSnapshot,
    PublishOutcome,
    open_snapshot,
    publish,
)
from crank.agents.jobs.matching import rank_listings
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG, RankingConfig
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.organization import Organization
from crank.models.preference import UserPreference
from crank.services import match_recompute
from crank.services.match_recompute import RecomputeStatus, recompute_user


def make_listing(source, organization, *, title="Engineer", status=JobListing.Status.ACTIVE):
    now = timezone.now()
    return JobListing.all_objects.create(
        source=source,
        external_id=f"{title.lower()}-{status}",
        canonical_url=f"https://jobs.example.test/{title.lower()}-{status}",
        employer_name=organization.name,
        title=title,
        first_seen_at=now - timedelta(days=1),
        last_seen_at=now,
        status=status,
        organization=organization,
    )


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class RecomputeUserTests(TestCase):
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
        self.listing = make_listing(self.source, self.organization)
        self.pref = UserPreference.objects.create(user=self.user, revision=0)

    def test_publishes_first_generation_from_db_inventory(self):
        """AC1: a committed preference produces results from DB inventory
        without any external source refresh call."""
        outcome = recompute_user(self.user, reason="preference")
        self.assertEqual(outcome.status, RecomputeStatus.PUBLISHED)
        self.assertEqual(outcome.persisted, 1)
        match = JobMatch.objects.get()
        self.assertEqual(match.result_generation, outcome.generation)
        state = MatchResultState.objects.get(user=self.user)
        self.assertEqual(state.current_generation, outcome.generation)
        self.assertEqual(state.preference_revision, 0)

    def test_second_call_with_no_changes_is_current(self):
        recompute_user(self.user, reason="preference")
        outcome = recompute_user(self.user, reason="drain")
        self.assertEqual(outcome.status, RecomputeStatus.CURRENT)
        state = MatchResultState.objects.get(user=self.user)
        # No new ticket issued for a no-op recompute.
        self.assertEqual(state.issued_generation, 1)

    def test_force_bypasses_currency_check(self):
        recompute_user(self.user, reason="preference")
        outcome = recompute_user(self.user, reason="drain", force=True)
        self.assertEqual(outcome.status, RecomputeStatus.PUBLISHED)
        state = MatchResultState.objects.get(user=self.user)
        self.assertEqual(state.issued_generation, 2)

    def test_no_preferences_short_circuits(self):
        other = User.objects.create_user("nopref", password="secret")
        outcome = recompute_user(other, reason="preference")
        self.assertEqual(outcome.status, RecomputeStatus.NO_PREFERENCES)
        # The state row may be race-safely created, but no ticket is issued
        # and no generation is published — no inventory read, no rows written.
        state = MatchResultState.objects.filter(user=other).first()
        if state is not None:
            self.assertIsNone(state.current_generation)
        self.assertFalse(JobMatch.objects.filter(user=other).exists())

    def test_recompute_never_raises_on_internal_failure(self):
        with patch(
            "crank.services.match_recompute.rank_listings",
            side_effect=RuntimeError("boom"),
        ):
            outcome = recompute_user(self.user, reason="preference")
        self.assertEqual(outcome.status, RecomputeStatus.FAILED)

    def test_unknown_user_id_is_user_gone(self):
        outcome = recompute_user(999999999, reason="drain")
        self.assertEqual(outcome.status, RecomputeStatus.USER_GONE)

    def test_accepts_a_user_id_int(self):
        outcome = recompute_user(self.user.pk, reason="drain")
        self.assertEqual(outcome.status, RecomputeStatus.PUBLISHED)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class RaceOrderingTests(TestCase):
    """AC2: an older run that snapshots first but publishes last is discarded."""

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
        self.listing = make_listing(self.source, self.organization)
        self.pref = UserPreference.objects.create(user=self.user, revision=0)

    def test_older_preference_snapshot_published_after_newer_is_discarded(self):
        old_snapshot = open_snapshot(self.user, max_listings=500)
        self.assertIsInstance(old_snapshot, MatchSnapshot)

        # A newer preference revision snapshots and publishes first.
        self.pref.revision = 1
        self.pref.save(update_fields=["revision", "modified"])
        new_snapshot = open_snapshot(self.user, max_listings=500)
        self.assertIsInstance(new_snapshot, MatchSnapshot)
        self.assertGreater(new_snapshot.ticket, old_snapshot.ticket)
        new_ranked = rank_listings(new_snapshot.listings, new_snapshot.criteria, DEFAULT_CONFIG)
        self.assertEqual(publish(new_snapshot, new_ranked), PublishOutcome.PUBLISHED)

        # The old (stale) run publishes last and must be discarded.
        old_ranked = rank_listings(old_snapshot.listings, old_snapshot.criteria, DEFAULT_CONFIG)
        self.assertEqual(publish(old_snapshot, old_ranked), PublishOutcome.DISCARDED_STALE)

        state = MatchResultState.objects.get(user=self.user)
        self.assertEqual(state.current_generation, new_snapshot.ticket)
        self.assertEqual(state.preference_revision, 1)
        match = JobMatch.objects.get()
        self.assertEqual(match.preference_revision, 1)

    def test_ranker_version_bump_only_current_version_rows_are_current(self):
        v1 = RankingConfig(version="1.0.0")
        v2 = RankingConfig(version="2.0.0")
        snapshot_v1 = open_snapshot(self.user, config=v1, max_listings=500)
        ranked_v1 = rank_listings(snapshot_v1.listings, snapshot_v1.criteria, v1)
        self.assertEqual(publish(snapshot_v1, ranked_v1), PublishOutcome.PUBLISHED)

        snapshot_v2 = open_snapshot(self.user, config=v2, max_listings=500)
        self.assertIsInstance(snapshot_v2, MatchSnapshot)
        ranked_v2 = rank_listings(snapshot_v2.listings, snapshot_v2.criteria, v2)
        self.assertEqual(publish(snapshot_v2, ranked_v2), PublishOutcome.PUBLISHED)

        self.assertEqual(JobMatch.objects.filter(ranker_version="2.0.0").count(), 1)
        state = MatchResultState.objects.get(user=self.user)
        self.assertEqual(state.ranker_version, "2.0.0")
        self.assertEqual(state.current_generation, snapshot_v2.ticket)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class DedupAndDismissalTests(TestCase):
    """AC3: repeated recompute never resets dismissals or duplicates rows."""

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
        self.listing = make_listing(self.source, self.organization)
        self.pref = UserPreference.objects.create(user=self.user, revision=0)

    def test_dismiss_and_recompute_repeatedly_preserves_flags_and_dedups(self):
        recompute_user(self.user, reason="preference")
        match = JobMatch.objects.get()
        match.dismissed = True
        match.seen_at = timezone.now()
        match.save(update_fields=["dismissed", "seen_at", "modified"])
        first_matched_at = match.first_matched_at

        for _ in range(3):
            outcome = recompute_user(self.user, reason="drain", force=True)
            self.assertEqual(outcome.status, RecomputeStatus.PUBLISHED)

        self.assertEqual(JobMatch.objects.count(), 1)
        match.refresh_from_db()
        self.assertTrue(match.dismissed)
        self.assertIsNotNone(match.seen_at)
        self.assertEqual(match.first_matched_at, first_matched_at)

    def test_ranker_bump_carries_forward_dismissed_and_seen(self):
        v1 = RankingConfig(version="1.0.0")
        recompute_user(self.user, reason="preference", config=v1)
        match = JobMatch.objects.get()
        match.dismissed = True
        seen_at = timezone.now()
        match.seen_at = seen_at
        match.save(update_fields=["dismissed", "seen_at", "modified"])

        v2 = RankingConfig(version="2.0.0")
        outcome = recompute_user(self.user, reason="drain", config=v2, force=True)
        self.assertEqual(outcome.status, RecomputeStatus.PUBLISHED)

        self.assertEqual(JobMatch.objects.count(), 2)
        new_row = JobMatch.objects.get(ranker_version="2.0.0")
        self.assertTrue(new_row.dismissed)
        self.assertEqual(new_row.seen_at, seen_at)

    def test_drain_twice_with_no_changes_second_is_duplicate_skipped(self):
        first = match_recompute.drain(10)
        self.assertEqual(first["users_succeeded"], 1)
        self.assertEqual(first["duplicate_skipped"], 0)

        # A second drain immediately after finds nothing pending (the user
        # is already current) — this is the ordinary no-op path.
        second = match_recompute.drain(10)
        self.assertEqual(second["users_total"], 0)
        state = MatchResultState.objects.get(user=self.user)
        self.assertEqual(state.issued_generation, 1)

        # duplicate_skipped covers the case where pending_users() flags a
        # user (e.g. the generation-dirty heuristic) but the snapshot then
        # finds it already CURRENT — exercised directly here.
        with patch(
            "crank.services.match_recompute.pending_users",
            return_value=[self.user.pk],
        ):
            third = match_recompute.drain(10)
        self.assertEqual(third["duplicate_skipped"], 1)
        self.assertEqual(state.issued_generation, 1)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class LifecycleTests(TestCase):
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
        self.listing = make_listing(self.source, self.organization)
        self.pref = UserPreference.objects.create(user=self.user, revision=0)

    def test_listing_closed_between_snapshot_and_publish_is_prefiltered(self):
        snapshot = open_snapshot(self.user, max_listings=500)
        ranked = rank_listings(snapshot.listings, snapshot.criteria, DEFAULT_CONFIG)
        self.listing.status = JobListing.Status.CLOSED
        self.listing.save(update_fields=["status", "modified"])
        outcome = publish(snapshot, ranked)
        self.assertEqual(outcome, PublishOutcome.PUBLISHED)
        self.assertFalse(JobMatch.objects.exists())

    def test_listing_deleted_before_publish_does_not_raise(self):
        snapshot = open_snapshot(self.user, max_listings=500)
        ranked = rank_listings(snapshot.listings, snapshot.criteria, DEFAULT_CONFIG)
        self.listing.delete()
        outcome = publish(snapshot, ranked)
        self.assertEqual(outcome, PublishOutcome.PUBLISHED)
        self.assertFalse(JobMatch.objects.exists())

    def test_user_deleted_between_snapshot_and_publish_is_user_gone(self):
        snapshot = open_snapshot(self.user, max_listings=500)
        ranked = rank_listings(snapshot.listings, snapshot.criteria, DEFAULT_CONFIG)
        self.user.delete()
        outcome = publish(snapshot, ranked)
        self.assertEqual(outcome, PublishOutcome.USER_GONE)

    def test_publish_failure_leaves_state_and_rows_unchanged_and_user_pending(self):
        recompute_user(self.user, reason="preference")
        state_before = MatchResultState.objects.get(user=self.user)
        snapshot = open_snapshot(self.user, max_listings=500, force=True)
        ranked = rank_listings(snapshot.listings, snapshot.criteria, DEFAULT_CONFIG)
        with patch(
            "crank.agents.jobs.match_persist.JobMatch.objects.bulk_update",
            side_effect=RuntimeError("db exploded"),
        ):
            outcome = publish(snapshot, ranked)
        self.assertEqual(outcome, PublishOutcome.FAILED)
        state_after = MatchResultState.objects.get(user=self.user)
        self.assertEqual(state_after.current_generation, state_before.current_generation)
        self.assertIn(self.user.pk, match_recompute.pending_users(10))

    def test_deadline_reached_mid_drain_leaves_remaining_pending(self):
        other = User.objects.create_user("second", password="secret")
        UserPreference.objects.create(user=other, revision=0)
        import time

        counts = match_recompute.drain(10, deadline=time.monotonic() - 1)
        self.assertTrue(counts["deadline_reached"])
        self.assertEqual(counts["users_succeeded"], 0)
        pending = match_recompute.pending_users(10)
        self.assertIn(self.user.pk, pending)
        self.assertIn(other.pk, pending)

    def test_drain_tallies_discarded_stale_and_no_preferences_and_failed(self):
        second = User.objects.create_user("second", password="secret")
        third = User.objects.create_user("third", password="secret")
        UserPreference.objects.create(user=second, revision=0)
        UserPreference.objects.create(user=third, revision=0)
        outcomes = {
            self.user.pk: match_recompute.RecomputeOutcome(
                status=RecomputeStatus.DISCARDED_STALE
            ),
            second.pk: match_recompute.RecomputeOutcome(
                status=RecomputeStatus.NO_PREFERENCES
            ),
            third.pk: match_recompute.RecomputeOutcome(status=RecomputeStatus.FAILED),
        }
        with patch(
            "crank.services.match_recompute.recompute_user",
            side_effect=lambda user, **kw: outcomes[user.pk],
        ):
            counts = match_recompute.drain(10)
        self.assertEqual(counts["stale_discarded"], 1)
        self.assertEqual(counts["users_failed"], 1)
        self.assertEqual(counts["users_succeeded"], 2)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class PendingUsersTests(TestCase):
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
        self.listing = make_listing(self.source, self.organization)

    def test_user_without_state_is_preference_dirty(self):
        UserPreference.objects.create(user=self.user, revision=0)
        self.assertIn(self.user.pk, match_recompute.pending_users(10))

    def test_current_user_is_not_pending(self):
        UserPreference.objects.create(user=self.user, revision=0)
        recompute_user(self.user, reason="preference")
        self.assertNotIn(self.user.pk, match_recompute.pending_users(10))

    def test_preference_revision_bump_makes_user_dirty_again(self):
        pref = UserPreference.objects.create(user=self.user, revision=0)
        recompute_user(self.user, reason="preference")
        self.assertNotIn(self.user.pk, match_recompute.pending_users(10))
        pref.revision = 1
        pref.save(update_fields=["revision", "modified"])
        self.assertIn(self.user.pk, match_recompute.pending_users(10))

    def test_stale_ranker_version_makes_user_generation_dirty(self):
        UserPreference.objects.create(user=self.user, revision=0)
        old_config = RankingConfig(version="0.0.1-old")
        recompute_user(self.user, reason="preference", config=old_config)
        state = MatchResultState.objects.get(user=self.user)
        self.assertNotEqual(state.ranker_version, DEFAULT_CONFIG.version)
        self.assertIn(self.user.pk, match_recompute.pending_users(10))

    def test_limit_zero_returns_empty(self):
        self.assertEqual(match_recompute.pending_users(0), [])

    def test_preference_dirty_tier_respects_limit(self):
        second = User.objects.create_user("second", password="secret")
        UserPreference.objects.create(user=self.user, revision=0)
        UserPreference.objects.create(user=second, revision=0)
        pending = match_recompute.pending_users(1)
        self.assertEqual(len(pending), 1)

    def test_dirty_user_is_not_starved_by_many_older_clean_users(self):
        """MAJOR review finding: the scan used to be capped and ordered
        oldest-modified-first, so a dirty user could be starved indefinitely
        by enough older clean users. The SQL-only selection has no cap."""
        UserPreference.objects.create(user=self.user, revision=0)
        recompute_user(self.user, reason="preference")
        self.assertNotIn(self.user.pk, match_recompute.pending_users(10))

        # Many older, clean (already-current) users.
        clean_users = []
        for i in range(250):
            u = User.objects.create_user(f"clean{i}", password="secret")
            pref = UserPreference.objects.create(user=u, revision=0)
            recompute_user(u, reason="preference")
            clean_users.append((u, pref))

        # Make the very first (oldest-modified) clean user's preference dirty
        # again by bumping its revision without recomputing.
        oldest_user, oldest_pref = clean_users[0]
        oldest_pref.revision = 1
        oldest_pref.save(update_fields=["revision", "modified"])

        pending = match_recompute.pending_users(1)
        self.assertIn(oldest_user.pk, pending)

    def test_deleted_preference_does_not_starve_generation_dirty_drain(self):
        """MAJOR review finding: a user whose preference row was deleted but
        who still has a stale/aged ``MatchResultState`` must not keep
        occupying generation-dirty drain slots forever."""
        UserPreference.objects.create(user=self.user, revision=0)
        recompute_user(self.user, reason="preference")
        state = MatchResultState.objects.get(user=self.user)
        # Force generation-dirty via an aged-out generated_at.
        old_time = timezone.now() - timedelta(hours=100)
        MatchResultState.objects.filter(pk=state.pk).update(generated_at=old_time)
        self.assertIn(self.user.pk, match_recompute.pending_users(10))

        UserPreference.objects.filter(user=self.user).delete()
        self.assertNotIn(self.user.pk, match_recompute.pending_users(10))

    def test_stale_data_watermark_makes_user_generation_dirty(self):
        from crank.models.publication import PublicationEvent
        from crank.services.publication import record_event

        UserPreference.objects.create(user=self.user, revision=0)
        recompute_user(self.user, reason="preference")
        self.assertNotIn(self.user.pk, match_recompute.pending_users(10))

        record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=self.organization.pk,
            event_kind=PublicationEvent.EventKind.CHANGED,
        )
        self.assertIn(self.user.pk, match_recompute.pending_users(10))


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class PendingCountsTests(TestCase):
    """Issue #475 review round 2, MINOR finding 5: ``--dry-run`` must report
    each dirtiness tier separately so the rollout gate (preference_dirty at
    zero) and the generation-dirty reason can both be checked, instead of
    one combined, limit-capped total."""

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
        self.listing = make_listing(self.source, self.organization)

    def test_zero_users_reports_all_zero(self):
        counts = match_recompute.pending_counts(10)
        self.assertEqual(counts["limit"], 10)
        self.assertEqual(counts["preference_dirty"], 0)
        self.assertEqual(counts["generation_dirty_data_stale"], 0)
        self.assertEqual(counts["generation_dirty_version_mismatch"], 0)
        self.assertEqual(counts["generation_dirty_interrupted"], 0)
        self.assertEqual(counts["generation_dirty_age_stale"], 0)
        self.assertEqual(counts["total_capped"], 0)

    def test_preference_dirty_is_not_capped_by_limit(self):
        """Unlike ``total_capped``, the per-tier counts must reflect the
        real total, not what a single bounded drain would claim."""
        for i in range(5):
            u = User.objects.create_user(f"dirty{i}", password="secret")
            UserPreference.objects.create(user=u, revision=0)
        counts = match_recompute.pending_counts(2)
        self.assertEqual(counts["preference_dirty"], 5)
        self.assertEqual(counts["total_capped"], 2)

    def test_ranker_version_mismatch_counted_separately_from_data_stale(self):
        UserPreference.objects.create(user=self.user, revision=0)
        old_config = RankingConfig(version="0.0.1-old")
        recompute_user(self.user, reason="preference", config=old_config)

        counts = match_recompute.pending_counts(10)
        self.assertEqual(counts["preference_dirty"], 0)
        self.assertEqual(counts["generation_dirty_version_mismatch"], 1)
        self.assertEqual(counts["generation_dirty_data_stale"], 0)
        self.assertEqual(counts["generation_dirty_age_stale"], 0)

    def test_data_stale_counted_separately_from_version_mismatch(self):
        from crank.models.publication import PublicationEvent
        from crank.services.publication import record_event

        UserPreference.objects.create(user=self.user, revision=0)
        recompute_user(self.user, reason="preference")
        record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=self.organization.pk,
            event_kind=PublicationEvent.EventKind.CHANGED,
        )

        counts = match_recompute.pending_counts(10)
        self.assertEqual(counts["generation_dirty_data_stale"], 1)
        self.assertEqual(counts["generation_dirty_version_mismatch"], 0)

    def test_age_stale_counted_separately(self):
        UserPreference.objects.create(user=self.user, revision=0)
        recompute_user(self.user, reason="preference")
        state = MatchResultState.objects.get(user=self.user)
        old_time = timezone.now() - timedelta(hours=100)
        MatchResultState.objects.filter(pk=state.pk).update(generated_at=old_time)

        counts = match_recompute.pending_counts(10)
        self.assertEqual(counts["generation_dirty_age_stale"], 1)
        self.assertEqual(counts["generation_dirty_data_stale"], 0)
        self.assertEqual(counts["generation_dirty_version_mismatch"], 0)

    def test_deleted_preference_excluded_from_generation_dirty_counts(self):
        UserPreference.objects.create(user=self.user, revision=0)
        recompute_user(self.user, reason="preference")
        state = MatchResultState.objects.get(user=self.user)
        old_time = timezone.now() - timedelta(hours=100)
        MatchResultState.objects.filter(pk=state.pk).update(generated_at=old_time)
        UserPreference.objects.filter(user=self.user).delete()

        counts = match_recompute.pending_counts(10)
        self.assertEqual(counts["generation_dirty_age_stale"], 0)
        self.assertEqual(counts["preference_dirty"], 0)


class RecomputeEnabledGateTests(TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(match_recompute.recompute_enabled())

    @override_settings(MATCH_RECOMPUTE_ENABLED=True)
    def test_enabled_flag_alone_is_enabled_absent_switch(self):
        self.assertTrue(match_recompute.recompute_enabled())

    @override_settings(MATCH_RECOMPUTE_ENABLED=True)
    def test_switch_off_blocks_even_with_flag_on(self):
        from crank.models.monitoring import CapabilitySwitch

        CapabilitySwitch.objects.create(key="match_recompute", enabled=False)
        self.assertFalse(match_recompute.recompute_enabled())


class OnPreferenceCommittedHookTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", password="secret")
        UserPreference.objects.create(user=self.user, revision=1)

    def test_noop_when_disabled(self):
        match_recompute.on_preference_committed(f"{self.user.pk}:1")
        self.assertFalse(MatchResultState.objects.filter(user=self.user).exists())

    @override_settings(MATCH_RECOMPUTE_ENABLED=True)
    def test_runs_recompute_when_enabled(self):
        match_recompute.on_preference_committed(f"{self.user.pk}:1")
        self.assertTrue(MatchResultState.objects.filter(user=self.user).exists())

    @override_settings(MATCH_RECOMPUTE_ENABLED=True)
    def test_unparseable_change_id_is_swallowed(self):
        match_recompute.on_preference_committed("not-a-valid-id")
        self.assertFalse(MatchResultState.objects.filter(user=self.user).exists())
