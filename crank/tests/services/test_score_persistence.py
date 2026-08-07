# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from unittest.mock import patch

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from crank.models.agent_run import AgentRun
from crank.models.organization import Organization
from crank.models.score import (
    Score,
    ScoreAlgorithm,
    ScoreAlgorithmWeight,
    ScoreType,
)
from crank.services import scores as score_services


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "score-persistence-tests",
        }
    }
)
class ScorePersistenceServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.source = Organization.objects.create(
            name="Source Org", gives_ratings=True
        )
        self.target = Organization.objects.create(name="Target Org")
        self.other_target = Organization.objects.create(name="Other Target")
        self.score_type = ScoreType.objects.create(name="Culture")
        self.other_type = ScoreType.objects.create(name="Compensation")
        self.algorithm = ScoreAlgorithm.objects.create(name="Overall")
        self.other_algorithm = ScoreAlgorithm.objects.create(name="Engagement")
        self.weight = ScoreAlgorithmWeight.objects.create(
            type=self.score_type, algorithm=self.algorithm, weight=1.0
        )
        ScoreAlgorithmWeight.objects.create(
            type=self.other_type, algorithm=self.other_algorithm, weight=1.0
        )
        self.run = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP,
            status=AgentRun.Status.SUCCEEDED,
            started_at=timezone.now(),
            finished_at=timezone.now(),
        )

    def _seed_affected_cache(self):
        """Populate every cache key a target/type write would invalidate."""
        for key in score_services.affected_cache_keys(
            self.target.id, self.score_type.id
        ):
            cache.set(key, {"stale": True})

    def _base_provenance(self, **overrides):
        prov = {
            "external_id": "ext-123",
            "source_url": "https://ratings.example.com/org/target-org",
            "adapter_version": "v1",
            "observed_at": "2026-08-07T00:00:00Z",
            "raw_value": "4.5",
        }
        prov.update(overrides)
        return prov

    # --- create ---

    def test_create_observation_creates_single_active_with_provenance_and_run(self):
        self._seed_affected_cache()
        with self.captureOnCommitCallbacks(execute=True):
            result = score_services.persist_score_observation(
                source=self.source,
                target=self.target,
                score_type=self.score_type,
                value=4.5,
                provenance=self._base_provenance(),
                run=self.run,
            )
        self.assertEqual(result.outcome, "created")
        self.assertTrue(result.created)
        self.assertIsNone(result.replaced)
        self.assertFalse(result.changed)
        active = Score.objects.filter(
            target=self.target,
            type=self.score_type,
            source=self.source,
            status=Score.ACTIVE_STATUS,
        )
        self.assertEqual(active.count(), 1)
        score = active.get()
        self.assertEqual(score.score, 4.5)
        self.assertEqual(score.low_threshold, 0.0)
        self.assertEqual(score.high_threshold, 5.0)
        self.assertEqual(score.run_id, self.run.id)
        self.assertEqual(score.provenance["external_id"], "ext-123")
        # Commit callback invalidated every affected cache key.
        for key in score_services.affected_cache_keys(
            self.target.id, self.score_type.id
        ):
            self.assertIsNone(cache.get(key))

    # --- no-op ---

    def test_identical_replay_is_noop_no_history_no_cache_work(self):
        score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=4.5,
            provenance=self._base_provenance(),
        )
        before = Score.objects.filter(target=self.target).count()
        self._seed_affected_cache()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            result = score_services.persist_score_observation(
                source=self.source,
                target=self.target,
                score_type=self.score_type,
                value=4.5,
                provenance=self._base_provenance(),
            )
        self.assertEqual(result.outcome, "noop")
        self.assertFalse(result.created)
        self.assertIsNone(result.replaced)
        # No history written and no on-commit invalidation scheduled.
        self.assertEqual(Score.objects.filter(target=self.target).count(), before)
        self.assertEqual(len(callbacks), 0)
        for key in score_services.affected_cache_keys(
            self.target.id, self.score_type.id
        ):
            self.assertEqual(cache.get(key), {"stale": True})

    def test_replay_matching_provenance_identity_ignores_timestamps(self):
        score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=4.5,
            provenance=self._base_provenance(observed_at="2026-08-07T00:00:00Z"),
        )
        result = score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=4.5,
            provenance=self._base_provenance(observed_at="2026-08-08T09:00:00Z"),
        )
        self.assertEqual(result.outcome, "noop")
        self.assertEqual(
            Score.objects.filter(target=self.target, status=1).count(), 1
        )

    # --- change ---

    def test_changed_observation_deactivates_old_and_creates_one_replacement(self):
        first = score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=3.0,
            provenance=self._base_provenance(),
        )
        self._seed_affected_cache()
        with self.captureOnCommitCallbacks(execute=True):
            result = score_services.persist_score_observation(
                source=self.source,
                target=self.target,
                score_type=self.score_type,
                value=4.5,
                provenance=self._base_provenance(raw_value="4.5"),
                run=self.run,
            )
        self.assertEqual(result.outcome, "changed")
        self.assertTrue(result.created)
        self.assertTrue(result.changed)
        self.assertEqual(result.replaced.id, first.score.id)
        first.score.refresh_from_db()
        self.assertEqual(first.score.status, Score.INACTIVE_STATUS)
        active = Score.objects.filter(
            target=self.target, type=self.score_type, status=1
        )
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.get().score, 4.5)
        self.assertEqual(active.get().run_id, self.run.id)
        for key in score_services.affected_cache_keys(
            self.target.id, self.score_type.id
        ):
            self.assertIsNone(cache.get(key))

    def test_change_preserves_history(self):
        score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=2.0,
            provenance=self._base_provenance(),
        )
        score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=5.0,
            provenance=self._base_provenance(raw_value="5.0"),
        )
        self.assertEqual(Score.objects.filter(target=self.target).count(), 2)
        self.assertEqual(
            Score.objects.filter(target=self.target, status=0).count(), 1
        )
        self.assertEqual(
            Score.objects.filter(target=self.target, status=1).count(), 1
        )

    # --- thresholds ---

    def test_changed_threshold_creates_replacement(self):
        score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=3.0,
            low_threshold=0.0,
            high_threshold=5.0,
            provenance=self._base_provenance(),
        )
        result = score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=3.0,
            low_threshold=0.0,
            high_threshold=10.0,
            provenance=self._base_provenance(),
        )
        self.assertEqual(result.outcome, "changed")
        active = Score.objects.get(
            target=self.target, type=self.score_type, status=1
        )
        self.assertEqual(active.high_threshold, 10.0)

    def test_matching_thresholds_are_noop(self):
        score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=3.0,
            low_threshold=1.0,
            high_threshold=9.0,
            provenance=self._base_provenance(),
        )
        result = score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=3.0,
            low_threshold=1.0,
            high_threshold=9.0,
            provenance=self._base_provenance(),
        )
        self.assertEqual(result.outcome, "noop")

    def test_value_change_is_not_noop(self):
        score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=3.0,
            provenance=self._base_provenance(),
        )
        result = score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=3.5,
            provenance=self._base_provenance(),
        )
        self.assertEqual(result.outcome, "changed")

    # --- provenance identity / sanitization ---

    def test_provenance_identity_change_triggers_change(self):
        score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=4.5,
            provenance=self._base_provenance(external_id="ext-123"),
        )
        result = score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=4.5,
            provenance=self._base_provenance(external_id="ext-456"),
        )
        self.assertEqual(result.outcome, "changed")
        self.assertEqual(
            Score.objects.get(target=self.target, status=1).provenance[
                "external_id"
            ],
            "ext-456",
        )

    def test_same_value_different_context_is_distinct_tuples(self):
        score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=4.5,
            provenance=self._base_provenance(),
        )
        result = score_services.persist_score_observation(
            source=self.source,
            target=self.other_target,
            score_type=self.score_type,
            value=4.5,
            provenance=self._base_provenance(),
        )
        self.assertEqual(result.outcome, "created")
        self.assertEqual(Score.objects.filter(status=1).count(), 2)

    def test_provenance_sanitization(self):
        result = score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=4.5,
            provenance={
                "external_id": "ext-123",
                "source_url": "https://x.io/org?token=deadbeefdeadbeefdeadbeefdeadbeef",
                "adapter_version": "v1",
                "notes": "secret=topsecret " + "x" * 1000,
                "unknown_key": "dropped",
                "nested": {"a": 1},
                "api_key": "this-is-an-api-secret",
                "email": "someone@example.com",
            },
        )
        prov = Score.objects.get(pk=result.score.id).provenance
        self.assertIn("external_id", prov)
        self.assertIn("source_url", prov)
        self.assertNotIn("deadbeef", prov["source_url"])
        self.assertNotIn("unknown_key", prov)
        self.assertNotIn("nested", prov)
        self.assertNotIn("api_key", prov)
        self.assertNotIn("someone@example.com", prov.get("notes", ""))
        self.assertLessEqual(len(prov["notes"]), 255)

    def test_sanitize_provenance_rejects_non_dict(self):
        self.assertEqual(score_services.sanitize_provenance(None), {})
        self.assertEqual(score_services.sanitize_provenance("oops"), {})

    def test_sanitize_provenance_rejects_objects_under_allowed_keys(self):
        # A non-scalar (list/dict) under an otherwise-allowed key is rejected so
        # provenance stays flat and JSON-friendly.
        result = score_services.sanitize_provenance(
            {
                "external_id": "ext-123",
                "source_url": ["https://x.io/1", "https://x.io/2"],
                "notes": {"secret": True},
            }
        )
        self.assertEqual(result, {"external_id": "ext-123"})

    def test_redact_non_string_is_returned_unchanged(self):
        self.assertEqual(score_services._redact(12345), 12345)
        self.assertEqual(score_services._redact(None), None)

    # --- run linking ---

    def test_run_provenance_is_linked(self):
        result = score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=4.0,
            run=self.run,
        )
        self.assertEqual(result.score.run_id, self.run.id)
        self.assertEqual(self.run.scores.count(), 1)

    # --- rollback / batch boundary ---

    def test_failed_batch_rolls_back_and_does_not_invalidate(self):
        self._seed_affected_cache()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            try:
                with transaction.atomic():
                    score_services.persist_score_observation(
                        source=self.source,
                        target=self.target,
                        score_type=self.score_type,
                        value=4.5,
                        provenance=self._base_provenance(),
                    )
                    raise RuntimeError("source adapter failed")
            except RuntimeError:
                pass
        # Nothing committed and no invalidation was scheduled.
        self.assertEqual(Score.objects.filter(target=self.target).count(), 0)
        self.assertEqual(len(callbacks), 0)
        for key in score_services.affected_cache_keys(
            self.target.id, self.score_type.id
        ):
            self.assertEqual(cache.get(key), {"stale": True})

    def test_batch_commits_once_and_invalidates_on_real_commit(self):
        self._seed_affected_cache()
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                score_services.persist_score_observation(
                    source=self.source,
                    target=self.target,
                    score_type=self.score_type,
                    value=3.0,
                    provenance=self._base_provenance(),
                )
                score_services.persist_score_observation(
                    source=self.source,
                    target=self.target,
                    score_type=self.score_type,
                    value=4.0,
                    provenance=self._base_provenance(raw_value="4.0"),
                )
        self.assertEqual(
            Score.objects.filter(target=self.target, status=1).count(), 1
        )
        for key in score_services.affected_cache_keys(
            self.target.id, self.score_type.id
        ):
            self.assertIsNone(cache.get(key))

    # --- uniqueness / concurrency ---

    def test_model_partial_unique_constraint_enforces_single_active(self):
        score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=4.5,
            provenance=self._base_provenance(),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Score.objects.create(
                    type=self.score_type,
                    source=self.source,
                    target=self.target,
                    score=9.0,
                    status=Score.ACTIVE_STATUS,
                )
        self.assertEqual(
            Score.objects.filter(target=self.target, status=1).count(), 1
        )

    def test_concurrent_first_write_retries_and_keeps_single_active(self):
        """Simulate a writer that loses the first-observation race.

        The partial unique constraint rejects the second concurrent active
        insert; the service must recover inside a fresh transaction and leave
        exactly one active row (no duplicate churn).
        """
        real_create = Score.objects.create
        calls = {"n": 0}

        def racing_create(**kwargs):
            # First attempt loses to a concurrent winner: the constraint fires.
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError(
                    "UNIQUE constraint failed: "
                    "crank_score.unique_score_type_source_target_status"
                )
            return real_create(**kwargs)

        with patch.object(Score.objects, "create", side_effect=racing_create):
            result = score_services.persist_score_observation(
                source=self.source,
                target=self.target,
                score_type=self.score_type,
                value=4.5,
                provenance=self._base_provenance(),
            )
        self.assertEqual(result.outcome, "created")
        self.assertEqual(Score.objects.filter(target=self.target).count(), 1)
        self.assertEqual(
            Score.objects.filter(target=self.target, status=1).count(), 1
        )

    def test_accepts_pks_instead_of_instances(self):
        result = score_services.persist_score_observation(
            source=self.source.id,
            target=self.target.id,
            score_type=self.score_type.id,
            value=4.0,
        )
        self.assertEqual(result.outcome, "created")
        score = result.score
        self.assertEqual(score.source_id, self.source.id)
        self.assertEqual(score.target_id, self.target.id)
        self.assertEqual(score.type_id, self.score_type.id)

    def test_rejects_unresolvable_identifier(self):
        with self.assertRaises(TypeError):
            score_services.persist_score_observation(
                source=object(),
                target=self.target,
                score_type=self.score_type,
                value=4.0,
            )


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "score-cache-tests",
        }
    }
)
class ScoreCacheKeyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.target = Organization.objects.create(name="Cache Target")
        self.score_type = ScoreType.objects.create(name="Culture")
        self.other_type = ScoreType.objects.create(name="Compensation")
        self.algo = ScoreAlgorithm.objects.create(name="Overall")
        self.algo2 = ScoreAlgorithm.objects.create(name="Engagement")
        self.algo3 = ScoreAlgorithm.objects.create(name="Unrelated")
        ScoreAlgorithmWeight.objects.create(
            type=self.score_type, algorithm=self.algo, weight=1.0
        )
        ScoreAlgorithmWeight.objects.create(
            type=self.score_type, algorithm=self.algo2, weight=0.5
        )
        ScoreAlgorithmWeight.objects.create(
            type=self.other_type, algorithm=self.algo3, weight=1.0
        )
        # Inactive algorithm results are never served, so they are not touched.
        self.inactive_algo = ScoreAlgorithm.objects.create(
            name="Inactive Algo", status=0
        )
        ScoreAlgorithmWeight.objects.create(
            type=self.score_type, algorithm=self.inactive_algo, weight=1.0
        )

    def test_every_known_cache_key(self):
        keys = score_services.affected_cache_keys(self.target.id, self.score_type.id)
        expected = {
            f"organization_{self.target.id}_avg_scores",
            f"organization_api_{self.target.id}",
            f"organization_scores_api_{self.target.id}",
            f"algorithm_{self.algo.id}_results",
            f"algorithm_{self.algo2.id}_results",
        }
        self.assertEqual(set(keys), expected)
        # Algorithms weighted on a different type are not invalidated by this
        # type's writes, and inactive algorithms are excluded.
        self.assertNotIn(f"algorithm_{self.algo3.id}_results", keys)
        self.assertNotIn(f"algorithm_{self.inactive_algo.id}_results", keys)

    def test_without_type_invalidates_all_active_algorithm_results(self):
        # When the changed type is unknown we cannot narrow the affected set, so
        # every active algorithm's result key is invalidated (none of the
        # algorithm-result keys are served for inactive algorithms).
        result = score_services.affected_cache_keys(self.target.id, None)
        self.assertEqual(
            set(result),
            {
                f"organization_{self.target.id}_avg_scores",
                f"organization_api_{self.target.id}",
                f"organization_scores_api_{self.target.id}",
                f"algorithm_{self.algo.id}_results",
                f"algorithm_{self.algo2.id}_results",
                f"algorithm_{self.algo3.id}_results",
            },
        )

    def test_invalidate_clears_every_known_key(self):
        keys = score_services.affected_cache_keys(self.target.id, self.score_type.id)
        for key in keys:
            cache.set(key, "stale")
        score_services.invalidate_score_caches(self.target.id, self.score_type.id)
        for key in keys:
            self.assertIsNone(cache.get(key))

    def test_invalidate_without_type_still_clears_algorithm_keys_for_type(self):
        # After a score write, only algorithms weighted on that type are
        # affected; invalidating without a type does not scan unrelated algos.
        keys = score_services.affected_cache_keys(self.target.id, None)
        for key in keys:
            cache.set(key, "stale")
        score_services.invalidate_score_caches(self.target.id, None)
        for key in keys:
            self.assertIsNone(cache.get(key))
