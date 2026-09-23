# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from datetime import timedelta

from django.contrib.auth.models import User
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from crank.agents.jobs.match_persist import (
    MatchSnapshot,
    PublishOutcome,
    _get_or_create_state,
    _within_age,
    open_snapshot,
    persist_matches,
    publish,
)
from crank.agents.jobs.matching import JobCriteria, MatchResult
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.organization import Organization
from crank.models.preference import UserPreference


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
class MatchPersistenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", password="secret")
        self.organization = Organization.objects.create(name="Acme")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
        )
        self.criteria = JobCriteria(criteria_version=7)
        self.listing = make_listing(self.source, self.organization)

    def test_upserts_idempotently_and_updates_freshness_and_factors(self):
        before = timezone.now()
        self.assertEqual(
            persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG), 1
        )
        match = JobMatch.objects.get()
        self.assertEqual(str(match), f"{self.listing} for {self.user}")
        self.assertGreaterEqual(match.first_matched_at, before)
        first_matched_at = match.first_matched_at
        first_factors = match.factors

        updated_criteria = JobCriteria(
            work_modes=frozenset({"remote"}), criteria_version=7
        )
        self.listing.is_remote = True
        self.listing.save(update_fields=["is_remote", "modified"])
        self.assertEqual(
            persist_matches(self.user, [self.listing], updated_criteria, DEFAULT_CONFIG), 1
        )
        match.refresh_from_db()
        self.assertEqual(JobMatch.objects.count(), 1)
        self.assertEqual(match.first_matched_at, first_matched_at)
        self.assertGreaterEqual(match.last_matched_at, first_matched_at)
        self.assertNotEqual(match.factors, first_factors)

    def test_unique_version_constraint_and_string_representation(self):
        persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG)
        match = JobMatch.objects.get()
        self.assertEqual(str(match), f"{self.listing} for {self.user}")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JobMatch.objects.create(
                    user=self.user,
                    listing=self.listing,
                    organization=self.organization,
                    preference_version=self.criteria.criteria_version,
                    ranker_version=DEFAULT_CONFIG.version,
                    score=1,
                    first_matched_at=timezone.now(),
                    last_matched_at=timezone.now(),
                )

    def test_skips_excluded_and_closed_or_expired_listings(self):
        excluded = make_listing(self.source, self.organization, title="Excluded")
        closed = make_listing(
            self.source, self.organization, title="Closed", status=JobListing.Status.CLOSED
        )
        expired = make_listing(
            self.source, self.organization, title="Expired", status=JobListing.Status.EXPIRED
        )
        criteria = JobCriteria(excluded_titles=frozenset({"excluded"}), criteria_version=7)

        self.assertEqual(
            persist_matches(
                self.user,
                [self.listing, excluded, closed, expired],
                criteria,
                DEFAULT_CONFIG,
            ),
            1,
        )
        self.assertEqual(JobMatch.objects.values_list("listing_id", flat=True).get(), self.listing.pk)

    @patch("crank.agents.jobs.match_persist.rank_listings")
    def test_ignores_rank_results_without_a_supplied_listing(self, rank_listings):
        rank_listings.return_value = [
            MatchResult(
                listing_id=999999,
                score=50.0,
                excluded=False,
                exclusion_reasons=[],
                factors=[],
                ranker_version=DEFAULT_CONFIG.version,
                criteria_version=self.criteria.criteria_version,
            )
        ]
        self.assertEqual(
            persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG), 0
        )
        self.assertFalse(JobMatch.objects.exists())

    def test_dismissed_match_stays_dismissed_when_refreshed(self):
        persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG)
        match = JobMatch.objects.get()
        match.dismissed = True
        match.save(update_fields=["dismissed", "modified"])

        persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG)
        match.refresh_from_db()
        self.assertTrue(match.dismissed)


    def test_preference_edit_updates_existing_row_preserving_seen_and_dismissed(self):
        """AC-12: changing the preference document (revision bump) and re-running
        updates the same row — preserving seen_at/dismissed — instead of
        inserting a second row. ``preference_version`` (schema) stays 7."""
        pref = UserPreference.objects.create(user=self.user, revision=0)
        persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG)
        match = JobMatch.objects.get()
        self.assertEqual(match.preference_revision, 0)
        seen_at = timezone.now()
        match.seen_at = seen_at
        match.save(update_fields=["seen_at", "modified"])

        # Simulate a preference edit: the document revision advances, the schema
        # version (and so the unique key's preference_version) does not.
        pref.revision = 1
        pref.save(update_fields=["revision", "modified"])

        self.assertEqual(
            persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG), 1
        )
        self.assertEqual(JobMatch.objects.count(), 1)
        match.refresh_from_db()
        self.assertEqual(match.preference_revision, 1)
        self.assertEqual(match.seen_at, seen_at)
        self.assertFalse(match.dismissed)

    def test_new_columns_are_written_on_created_branch(self):
        UserPreference.objects.create(user=self.user, revision=3)
        persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG)
        match = JobMatch.objects.get()
        self.assertEqual(match.preference_revision, 3)
        self.assertIsNotNone(match.generated_at)
        self.assertEqual(match.requirements, [])
        self.assertEqual(match.evidence_ids, [])
        self.assertIsNone(match.data_revision)

    def test_data_revision_includes_listing_publication_events(self):
        """data_revision is the greatest PublicationEvent.id touching the row,
        counting both the organization's events and the listing's **source**
        ingest events (issue #475 G3 fix: the only LISTING-event writer,
        ``record_source_publication``, writes ``target_id=source.pk``, so
        LISTING events must be looked up by source id, not listing id)."""
        from crank.services.publication import record_event
        from crank.models.publication import PublicationEvent

        # Organization event and a later source-keyed listing ingest event.
        org_event = record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=self.organization.pk,
            event_kind=PublicationEvent.EventKind.CHANGED,
        )
        listing_event = record_event(
            target_type=PublicationEvent.TargetType.LISTING,
            target_id=self.source.pk,
            event_kind=PublicationEvent.EventKind.INGESTED,
        )
        UserPreference.objects.create(user=self.user, revision=0)
        persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG)
        match = JobMatch.objects.get()
        # The source's ingest event must win over the organization's event.
        self.assertGreater(listing_event.pk, org_event.pk)
        self.assertEqual(match.data_revision, listing_event.pk)

    def test_data_revision_ignores_events_keyed_by_listing_pk(self):
        """A LISTING event keyed by ``listing.pk`` (not ``source.pk``) is a
        different target id and must never contribute to data_revision — the
        per-source writer contract (``docs/publication-outbox.md``) rejects
        per-listing events."""
        from crank.services.publication import record_event
        from crank.models.publication import PublicationEvent

        if self.listing.pk == self.source.pk:
            # Force distinct pks: both tables can otherwise start at 1.
            self.listing = make_listing(
                self.source, self.organization, title="Engineer2"
            )
        self.assertNotEqual(self.listing.pk, self.source.pk)
        record_event(
            target_type=PublicationEvent.TargetType.LISTING,
            target_id=self.listing.pk,
            event_kind=PublicationEvent.EventKind.INGESTED,
        )
        UserPreference.objects.create(user=self.user, revision=0)
        persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG)
        match = JobMatch.objects.get()
        self.assertIsNone(match.data_revision)

    def test_data_revision_includes_score_events(self):
        """A SCORE event on the listing's organization contributes to
        data_revision because organization scores feed the fit score."""
        from crank.services.publication import record_event
        from crank.models.publication import PublicationEvent

        score_event = record_event(
            target_type=PublicationEvent.TargetType.SCORE,
            target_id=self.organization.pk,
            event_kind=PublicationEvent.EventKind.OBSERVED,
        )
        UserPreference.objects.create(user=self.user, revision=0)
        persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG)
        match = JobMatch.objects.get()
        self.assertEqual(match.data_revision, score_event.pk)

    def test_persist_matches_returns_zero_when_publish_is_not_published(self):
        """issue #475: the legacy wrapper reports 0 whenever the CAS publish
        is discarded/fails, not just when nothing matched."""
        UserPreference.objects.create(user=self.user, revision=0)
        with patch(
            "crank.agents.jobs.match_persist.publish",
            return_value=PublishOutcome.DISCARDED_STALE,
        ):
            self.assertEqual(
                persist_matches(self.user, [self.listing], self.criteria, DEFAULT_CONFIG),
                0,
            )


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class MatchPersistInternalsTests(TestCase):
    """issue #475: direct coverage of small snapshot/publish primitives."""

    def setUp(self):
        self.user = User.objects.create_user("owner2", password="secret")
        self.organization = Organization.objects.create(name="Acme2")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic3",
            adapter_key="synthetic.v3",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        self.listing = make_listing(self.source, self.organization)

    def test_within_age_none_generated_at_is_false(self):
        self.assertFalse(_within_age(None, 24))

    def test_within_age_zero_disables_backstop(self):
        self.assertTrue(_within_age(timezone.now() - timedelta(days=365), 0))

    def test_get_or_create_state_recovers_from_concurrent_create(self):
        MatchResultState.objects.create(user=self.user)
        with patch(
            "crank.agents.jobs.match_persist.MatchResultState.objects.filter"
        ) as filtered:
            filtered.return_value.first.return_value = None
            with patch(
                "crank.agents.jobs.match_persist.MatchResultState.objects.create",
                side_effect=RuntimeError("unique constraint"),
            ):
                state = _get_or_create_state(self.user)
        self.assertEqual(state.user_id, self.user.pk)

    def test_publish_discards_when_defensive_preference_check_trips(self):
        """A snapshot whose preference_revision is behind the state's
        stamped value is discarded even when its ticket is ahead (the
        defensive check, distinct from the ticket comparison)."""
        UserPreference.objects.create(user=self.user, revision=5)
        snapshot = open_snapshot(self.user, max_listings=500)
        from crank.agents.jobs.matching import rank_listings
        from crank.agents.jobs.ranking_config import DEFAULT_CONFIG

        ranked = rank_listings(snapshot.listings, snapshot.criteria, DEFAULT_CONFIG)
        self.assertEqual(publish(snapshot, ranked), PublishOutcome.PUBLISHED)

        stale_snapshot = MatchSnapshot(
            user_id=snapshot.user_id,
            ticket=snapshot.ticket + 5,
            preference_revision=0,
            preference_version=snapshot.preference_version,
            ranker_version=snapshot.ranker_version,
            data_revision=snapshot.data_revision,
            listings=snapshot.listings,
            criteria=snapshot.criteria,
            evidence=snapshot.evidence,
            data_revisions=snapshot.data_revisions,
        )
        self.assertEqual(
            publish(stale_snapshot, ranked), PublishOutcome.DISCARDED_STALE
        )

    def test_publish_skips_ranked_listing_not_in_the_snapshot(self):
        """A ranked result referencing a real, unmatched listing id outside
        the snapshot's inventory is skipped, not persisted."""
        from crank.agents.jobs.matching import MatchResult

        outside_listing = make_listing(self.source, self.organization, title="Outside")
        UserPreference.objects.create(user=self.user, revision=0)
        snapshot = open_snapshot(self.user, max_listings=0)
        self.assertEqual(snapshot.listings, [])
        fake_result = MatchResult(
            listing_id=outside_listing.pk,
            score=10.0,
            excluded=False,
            exclusion_reasons=[],
            factors=[],
            ranker_version=DEFAULT_CONFIG.version,
            criteria_version=snapshot.preference_version,
        )
        self.assertEqual(publish(snapshot, [fake_result]), PublishOutcome.PUBLISHED)
        self.assertFalse(JobMatch.objects.exists())
