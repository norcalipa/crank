# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the transactional publication outbox (issue #470).

SQLite is the CI backend; the MySQL variants (real row locks, conditional
update contention) are documented for ops in docs/publication-outbox.md.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings

from crank.models.organization import Organization
from crank.models.publication import PublicationEvent
from crank.models.score import ScoreAlgorithm, ScoreAlgorithmWeight, ScoreType
from crank.services import publication
from crank.services import scores as score_services


def locmem(location):
    return override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": location,
            }
        }
    )


@locmem("publication-record-tests")
class RecordEventTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Outbox Target")

    def test_record_event_writes_revision_row_with_allowlisted_payload(self):
        event = publication.record_event(
            target_type=PublicationEvent.TargetType.SCORE,
            target_id=self.org.id,
            event_kind=PublicationEvent.EventKind.CREATED,
            payload={
                "score_type_id": 3,
                "source_id": 4,
                "outcome": "created",
                "unknown_key": "dropped",
            },
        )
        self.assertEqual(event.pk, PublicationEvent.objects.get().pk)
        self.assertIsNone(event.processed_at)
        self.assertEqual(
            event.payload,
            {"score_type_id": 3, "source_id": 4, "outcome": "created"},
        )

    def test_record_event_rejects_unknown_target_type_kind_and_ids(self):
        with self.assertRaises(ValueError):
            publication.record_event(
                target_type="user",
                target_id=1,
                event_kind="created",
            )
        with self.assertRaises(ValueError):
            publication.record_event(
                target_type=PublicationEvent.TargetType.SCORE,
                target_id=1,
                event_kind="deleted",
            )
        with self.assertRaises(ValueError):
            publication.record_event(
                target_type=PublicationEvent.TargetType.SCORE,
                target_id=-1,
                event_kind="created",
            )

    def test_record_event_rejects_non_dict_payload(self):
        event = publication.record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=self.org.id,
            event_kind="observed",
            payload=["not", "a", "dict"],
        )
        self.assertEqual(event.payload, {})

    def test_record_event_bounds_strings_and_drops_unknown_keys(self):
        event = publication.record_event(
            target_type=PublicationEvent.TargetType.LISTING,
            target_id=1,
            event_kind="ingested",
            payload={
                "source_key": "x" * 1000,
                "outcome": "y" * 1000,
                "ingested": "5",
                "extra": "dropped",
            },
        )
        self.assertEqual(len(event.payload["source_key"]), 255)
        self.assertEqual(len(event.payload["outcome"]), 255)
        self.assertNotIn("ingested", event.payload)  # string, not int: dropped
        self.assertNotIn("extra", event.payload)

    def test_record_event_never_truncates_organization_ids(self):
        """Payload lists are bounded by construction, never silently
        truncated: an oversized list fails loudly so the caller emits chunked
        events instead of dropping organization invalidations (review
        finding: truncated listing targets)."""
        with self.assertRaises(ValueError):
            publication.record_event(
                target_type=PublicationEvent.TargetType.LISTING,
                target_id=1,
                event_kind="ingested",
                payload={
                    "organization_ids": list(
                        range(publication.MAX_PAYLOAD_ORGANIZATION_IDS + 1)
                    )
                },
            )
        event = publication.record_event(
            target_type=PublicationEvent.TargetType.LISTING,
            target_id=1,
            event_kind="ingested",
            payload={
                "organization_ids": list(
                    range(publication.MAX_PAYLOAD_ORGANIZATION_IDS)
                )
            },
        )
        self.assertEqual(
            len(event.payload["organization_ids"]),
            publication.MAX_PAYLOAD_ORGANIZATION_IDS,
        )

    def test_record_event_rejects_bools_and_non_list_org_ids(self):
        event = publication.record_event(
            target_type=PublicationEvent.TargetType.LISTING,
            target_id=1,
            event_kind="ingested",
            payload={"ingested": True, "organization_ids": "nope"},
        )
        self.assertEqual(event.payload, {})


@locmem("publication-sweep-tests")
class SweepTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Sweep Target")
        self.score_type = ScoreType.objects.create(name="Culture")
        self.algorithm = ScoreAlgorithm.objects.create(name="Overall")
        ScoreAlgorithmWeight.objects.create(
            type=self.score_type, algorithm=self.algorithm, weight=1.0
        )

    def _seed_keys(self, keys):
        for key in keys:
            cache.set(key, {"stale": True})

    def test_sweep_dedupes_keys_and_deletes_each_once(self):
        events = [
            publication.record_event(
                target_type=PublicationEvent.TargetType.SCORE,
                target_id=self.org.id,
                event_kind="changed",
                payload={"score_type_id": self.score_type.id, "outcome": "changed"},
            )
            for _ in range(3)
        ]
        keys = set(publication.affected_keys(events[-1]))
        self._seed_keys(keys)
        with patch.object(
            cache, "delete", wraps=cache.delete
        ) as delete:
            counts = publication.sweep_pending()
        self.assertEqual(counts["scanned"], 3)
        self.assertEqual(counts["processed"], 3)
        self.assertEqual(counts["keys_deleted"], len(keys))
        # Each affected key deleted exactly once despite duplicate events.
        self.assertEqual(delete.call_count, len(keys))
        self.assertEqual(
            sorted(call.args[0] for call in delete.call_args_list), sorted(keys)
        )
        for key in keys:
            self.assertIsNone(cache.get(key))
        self.assertEqual(
            PublicationEvent.objects.filter(processed_at__isnull=True).count(), 0
        )

    def test_sweep_unions_keys_across_same_target_events(self):
        """Pending events for the same target can carry different
        affected-key sets; the sweep must union keys across every selected
        event so no required invalidation is lost (review finding: lossy
        sweep deduplication)."""
        other_type = ScoreType.objects.create(name="Pay")
        pay_algorithm = ScoreAlgorithm.objects.create(name="Pay Algo")
        ScoreAlgorithmWeight.objects.create(
            type=other_type, algorithm=pay_algorithm, weight=1.0
        )
        publication.record_event(
            target_type=PublicationEvent.TargetType.SCORE,
            target_id=self.org.id,
            event_kind="changed",
            payload={"score_type_id": self.score_type.id, "outcome": "changed"},
        )
        publication.record_event(
            target_type=PublicationEvent.TargetType.SCORE,
            target_id=self.org.id,
            event_kind="changed",
            payload={"score_type_id": other_type.id, "outcome": "changed"},
        )
        culture_keys = set(
            score_services.affected_cache_keys(self.org.id, self.score_type.id)
        )
        pay_keys = set(score_services.affected_cache_keys(self.org.id, other_type.id))
        self.assertNotIn(f"algorithm_{pay_algorithm.id}_results", culture_keys)
        self._seed_keys(culture_keys | pay_keys)
        counts = publication.sweep_pending()
        self.assertEqual(counts["scanned"], 2)
        self.assertEqual(counts["processed"], 2)
        for key in culture_keys | pay_keys:
            self.assertIsNone(cache.get(key))

    def test_sweep_unions_listing_chunks_for_same_source(self):
        """Chunked listing events for one source each contribute their own
        organization ids; the sweep must invalidate every chunk's
        organizations (review finding: truncated listing targets)."""
        org_b = Organization.objects.create(name="Chunk B")
        org_c = Organization.objects.create(name="Chunk C")
        for chunk_index, ids in enumerate(([self.org.id, org_b.id], [org_c.id])):
            publication.record_event(
                target_type=PublicationEvent.TargetType.LISTING,
                target_id=99,
                event_kind="ingested",
                payload={
                    "organization_ids": ids,
                    "chunk_index": chunk_index,
                    "chunk_count": 2,
                },
            )
        keys = set()
        for organization_id in (self.org.id, org_b.id, org_c.id):
            keys.update(score_services.affected_cache_keys(organization_id, None))
        self._seed_keys(keys)
        counts = publication.sweep_pending()
        self.assertEqual(counts["processed"], 2)
        for key in keys:
            self.assertIsNone(cache.get(key))

    def test_score_event_sweep_clears_full_page_cache_key(self):
        """The full-page shell cache for /algo/<id>/ is an affected key, so
        a score publication sweep clears the rendered ranking page together
        with the result keys (review finding: uninvalidated full-page cache)."""
        page_key = f"algorithm_{self.algorithm.id}_page"
        cache.set(page_key, {"html": "stale ranking"})
        publication.record_event(
            target_type=PublicationEvent.TargetType.SCORE,
            target_id=self.org.id,
            event_kind="changed",
            payload={"score_type_id": self.score_type.id, "outcome": "changed"},
        )
        publication.sweep_pending()
        self.assertIsNone(cache.get(page_key))

    def test_sweep_rerun_is_noop(self):
        publication.record_event(
            target_type=PublicationEvent.TargetType.SCORE,
            target_id=self.org.id,
            event_kind="created",
            payload={"score_type_id": self.score_type.id, "outcome": "created"},
        )
        first = publication.sweep_pending()
        self.assertEqual(first["processed"], 1)
        second = publication.sweep_pending()
        self.assertEqual(second, {"scanned": 0, "processed": 0, "keys_deleted": 0})

    def test_sweep_only_marks_rows_still_pending(self):
        """Concurrent-sweep safety: rows processed by another sweep between
        our batch selection and our conditional update are never re-marked."""
        first = publication.record_event(
            target_type=PublicationEvent.TargetType.SCORE,
            target_id=self.org.id,
            event_kind="created",
            payload={"score_type_id": self.score_type.id, "outcome": "created"},
        )
        second = publication.record_event(
            target_type=PublicationEvent.TargetType.SCORE,
            target_id=self.org.id,
            event_kind="changed",
            payload={"score_type_id": self.score_type.id, "outcome": "changed"},
        )
        real_delete = cache.delete

        def racing_delete(key):
            # A concurrent sweep marks `first` processed while our sweep is
            # between batch selection and its conditional processed_at update.
            PublicationEvent.objects.filter(
                pk=first.pk, processed_at__isnull=True
            ).update(processed_at=first.created_at)
            return real_delete(key)

        with patch.object(cache, "delete", side_effect=racing_delete):
            counts = publication.sweep_pending()
        self.assertEqual(counts["scanned"], 2)
        self.assertEqual(counts["processed"], 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNotNone(first.processed_at)
        self.assertIsNotNone(second.processed_at)

    def test_sweep_respects_limit(self):
        for _ in range(3):
            publication.record_event(
                target_type=PublicationEvent.TargetType.ORGANIZATION,
                target_id=self.org.id,
                event_kind="observed",
            )
        counts = publication.sweep_pending(limit=2)
        self.assertEqual(counts["scanned"], 2)
        self.assertEqual(counts["processed"], 2)
        self.assertEqual(
            PublicationEvent.objects.filter(processed_at__isnull=True).count(), 1
        )

    def test_affected_keys_for_score_event_narrow_to_weighted_algorithms(self):
        event = publication.record_event(
            target_type=PublicationEvent.TargetType.SCORE,
            target_id=self.org.id,
            event_kind="changed",
            payload={"score_type_id": self.score_type.id, "outcome": "changed"},
        )
        keys = publication.affected_keys(event)
        self.assertIn(f"organization_provenance_api_{self.org.id}", keys)
        self.assertIn(
            score_services.algorithm_results_cache_key(self.algorithm.id), keys
        )
        self.assertIn(f"algorithm_{self.algorithm.id}_page", keys)

    def test_affected_keys_for_organization_event_include_provenance_key(self):
        event = publication.record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=self.org.id,
            event_kind="observed",
        )
        keys = publication.affected_keys(event)
        self.assertIn(f"organization_provenance_api_{self.org.id}", keys)
        self.assertIn(f"organization_api_{self.org.id}", keys)

    def test_affected_keys_for_listing_event_cover_payload_organizations(self):
        other = Organization.objects.create(name="Listing Employer")
        event = publication.record_event(
            target_type=PublicationEvent.TargetType.LISTING,
            target_id=99,  # source id; not an organization
            event_kind="ingested",
            payload={"organization_ids": [self.org.id, other.id]},
        )
        keys = publication.affected_keys(event)
        self.assertIn(f"organization_provenance_api_{self.org.id}", keys)
        self.assertIn(f"organization_api_{other.id}", keys)

    def test_organization_event_sweep_clears_provenance_key(self):
        provenance_key = f"organization_provenance_api_{self.org.id}"
        self._seed_keys([provenance_key])
        publication.record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=self.org.id,
            event_kind="observed",
        )
        publication.sweep_pending()
        self.assertIsNone(cache.get(provenance_key))

    def test_sweep_survives_events_for_deleted_targets(self):
        """No FKs to data rows: sweeps never fail for vanished targets."""
        publication.record_event(
            target_type=PublicationEvent.TargetType.SCORE,
            target_id=424242,
            event_kind="created",
            payload={"score_type_id": 987654, "outcome": "created"},
        )
        counts = publication.sweep_pending()
        self.assertEqual(counts["processed"], 1)


@locmem("publication-boundary-tests")
class TransactionBoundaryTests(TestCase):
    """Rollback/commit boundary and crash-after-commit recovery."""

    def setUp(self):
        cache.clear()
        self.source = Organization.objects.create(name="Boundary Source", gives_ratings=True)
        self.target = Organization.objects.create(name="Boundary Target")
        self.score_type = ScoreType.objects.create(name="Culture")
        self.algorithm = ScoreAlgorithm.objects.create(name="Overall")
        ScoreAlgorithmWeight.objects.create(
            type=self.score_type, algorithm=self.algorithm, weight=1.0
        )

    def _seed_keys(self):
        for key in score_services.affected_cache_keys(
            self.target.id, self.score_type.id
        ):
            cache.set(key, {"stale": True})

    def _persist(self):
        return score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=4.5,
            provenance={"external_id": "ext-1", "adapter_version": "v1"},
        )

    def test_rollback_writes_no_event_and_deletes_no_cache(self):
        self._seed_keys()
        with self.captureOnCommitCallbacks(execute=False) as callbacks, self.assertRaises(
            RuntimeError
        ), transaction.atomic():
            self._persist()
            raise RuntimeError("source adapter failed")
        self.assertEqual(PublicationEvent.objects.count(), 0)
        self.assertEqual(len(callbacks), 0)
        for key in score_services.affected_cache_keys(
            self.target.id, self.score_type.id
        ):
            self.assertEqual(cache.get(key), {"stale": True})

    def test_commit_records_exactly_one_event(self):
        self._seed_keys()
        with self.captureOnCommitCallbacks(execute=True):
            self._persist()
        events = PublicationEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.target_type, PublicationEvent.TargetType.SCORE)
        self.assertEqual(event.target_id, self.target.id)
        self.assertEqual(event.event_kind, PublicationEvent.EventKind.CREATED)
        self.assertEqual(event.payload["outcome"], "created")
        self.assertEqual(event.payload["score_type_id"], self.score_type.id)
        # Fast-path invalidation already ran on commit; the event stays
        # pending until the sweep marks it.
        self.assertIsNone(event.processed_at)

    def test_crash_after_commit_recovers_on_sweep(self):
        """on_commit never fires (worker died): the sweep closes the loss."""
        self._seed_keys()
        with self.captureOnCommitCallbacks(execute=False):
            self._persist()
        # Commit happened, but no invalidation ran: caches are stale and one
        # durable event is pending.
        self.assertEqual(PublicationEvent.objects.count(), 1)
        for key in score_services.affected_cache_keys(
            self.target.id, self.score_type.id
        ):
            self.assertEqual(cache.get(key), {"stale": True})
        counts = publication.sweep_pending()
        self.assertEqual(counts["processed"], 1)
        for key in score_services.affected_cache_keys(
            self.target.id, self.score_type.id
        ):
            self.assertIsNone(cache.get(key))

    def test_events_survive_target_deletion(self):
        """No FKs to data rows: events survive when their target vanishes.

        (Score rows RESTRICT organization deletion, but events carry no FK at
        all — an event for a deleted target sweeps harmlessly.)
        """
        target = Organization.objects.create(name="Doomed Target")
        publication.record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=target.id,
            event_kind="observed",
        )
        target.delete()
        self.assertEqual(PublicationEvent.objects.count(), 1)
        counts = publication.sweep_pending()
        self.assertEqual(counts["processed"], 1)
        self.assertIsNotNone(
            PublicationEvent.objects.get().processed_at
        )


@locmem("publication-watermark-tests")
class DataWatermarkTests(TestCase):
    """issue #475: the coarse data-dirtiness watermark for match recompute."""

    def test_watermark_is_none_with_no_events(self):
        self.assertIsNone(publication.data_watermark())

    def test_watermark_is_the_highest_event_id(self):
        org = Organization.objects.create(name="Watermark Org")
        publication.record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=org.id,
            event_kind="observed",
        )
        last = publication.record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=org.id,
            event_kind="changed",
        )
        self.assertEqual(publication.data_watermark(), last.pk)


@locmem("publication-organization-data-revisions-tests")
class OrganizationDataRevisionsTests(TestCase):
    """issue #475 G3: organization_data_revisions() also covers SCORE events."""

    def test_includes_score_events(self):
        org = Organization.objects.create(name="Score Revision Org")
        org_event = publication.record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=org.id,
            event_kind="changed",
        )
        score_event = publication.record_event(
            target_type=PublicationEvent.TargetType.SCORE,
            target_id=org.id,
            event_kind="observed",
        )
        self.assertGreater(score_event.pk, org_event.pk)
        revisions = publication.organization_data_revisions([org.id])
        self.assertEqual(revisions[org.id], score_event.pk)

    def test_organization_with_no_events_is_omitted(self):
        org = Organization.objects.create(name="Untouched Org")
        self.assertEqual(publication.organization_data_revisions([org.id]), {})
