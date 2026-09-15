# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import (
    IntegrityError,
    connection,
    connections,
    transaction,
)
from django.test import (
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.utils import timezone

from crank.models.agent_run import AgentRun
from crank.models.organization import Organization
from crank.models.score import (
    Score,
    ScoreAlgorithm,
    ScoreAlgorithmWeight,
    ScoreTupleAnchor,
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
        version = score_services.SCORE_CACHE_KEY_VERSION
        expected = {
            f"{version}:organization_{self.target.id}_avg_scores",
            f"organization_api_{self.target.id}",
            f"{version}:organization_scores_api_{self.target.id}",
            f"{version}:algorithm_{self.algo.id}_results",
            f"{version}:algorithm_{self.algo2.id}_results",
        }
        self.assertEqual(set(keys), expected)
        # Algorithms weighted on a different type are not invalidated by this
        # type's writes, and inactive algorithms are excluded.
        self.assertNotIn(f"{version}:algorithm_{self.algo3.id}_results", keys)
        self.assertNotIn(f"{version}:algorithm_{self.inactive_algo.id}_results", keys)
        # Every score-derived key is versioned away from its pre-#461 name, so
        # a deploy can never serve a stale pre-fix cache entry (the
        # organization-detail key serves attributes only and intentionally
        # stays unversioned).
        self.assertNotIn(f"organization_{self.target.id}_avg_scores", keys)
        self.assertNotIn(f"organization_scores_api_{self.target.id}", keys)
        self.assertNotIn(f"algorithm_{self.algo.id}_results", keys)

    def test_without_type_invalidates_all_active_algorithm_results(self):
        # When the changed type is unknown we cannot narrow the affected set, so
        # every active algorithm's result key is invalidated (none of the
        # algorithm-result keys are served for inactive algorithms).
        version = score_services.SCORE_CACHE_KEY_VERSION
        result = score_services.affected_cache_keys(self.target.id, None)
        self.assertEqual(
            set(result),
            {
                f"{version}:organization_{self.target.id}_avg_scores",
                f"organization_api_{self.target.id}",
                f"{version}:organization_scores_api_{self.target.id}",
                f"{version}:algorithm_{self.algo.id}_results",
                f"{version}:algorithm_{self.algo2.id}_results",
                f"{version}:algorithm_{self.algo3.id}_results",
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


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "active-score-summary-tests",
        }
    }
)
class ActiveScoreSummaryRowsTests(TestCase):
    """Read-side helper tests: active scores of active types only."""

    def setUp(self):
        self.source = Organization.objects.create(name="Source Org", gives_ratings=True)
        self.target = Organization.objects.create(name="Target Org")
        self.other_target = Organization.objects.create(name="Other Target")
        self.type_a = ScoreType.objects.create(name="Culture")
        self.type_b = ScoreType.objects.create(name="Compensation")
        self.retired_type = ScoreType.objects.create(name="Legacy", status=0)

    def _target_scores(self, **overrides):
        defaults = {
            "source": self.source,
            "target": self.target,
            "type": self.type_a,
        }
        defaults.update(overrides)
        return Score.objects.create(**defaults)

    def test_excludes_superseded_rows_and_inactive_types(self):
        self._target_scores(score=1.0, status=0)  # superseded
        self._target_scores(score=5.0)  # active replacement
        self._target_scores(score=4.0, type=self.retired_type)  # retired type
        rows = score_services.active_score_summary_rows(target_ids=[self.target.id])
        self.assertEqual(
            list(rows),
            [{"target_id": self.target.id, "type__name": "Culture", "avg_score": 5.0}],
        )

    def test_multiple_active_scores_of_one_type_averaged(self):
        self._target_scores(score=4.0)
        # A second active row from a different source (the partial unique
        # constraint allows only one active row per type/source/target).
        other_source = Organization.objects.create(
            name="Second Source", gives_ratings=True
        )
        self._target_scores(score=6.0, source=other_source)
        rows = score_services.active_score_summary_rows(target_ids=[self.target.id])
        self.assertEqual(rows[0]["avg_score"], 5.0)

    def test_filters_by_target_ids(self):
        self._target_scores(score=5.0)
        Score.objects.create(
            source=self.source, target=self.other_target, type=self.type_a, score=1.0
        )
        rows = score_services.active_score_summary_rows(target_ids=[self.target.id])
        self.assertEqual([row["target_id"] for row in rows], [self.target.id])

    def test_score_types_filter_restricts_to_named_active_types(self):
        self._target_scores(score=5.0)
        self._target_scores(score=2.0, type=self.type_b)
        self._target_scores(score=3.0, type=self.retired_type)
        rows = score_services.active_score_summary_rows(
            target_ids=[self.target.id], score_types=["Compensation"]
        )
        self.assertEqual(
            [row["type__name"] for row in rows], ["Compensation"]
        )
        self.assertEqual(rows[0]["avg_score"], 2.0)

    def test_empty_score_set_yields_no_rows(self):
        self.assertEqual(list(score_services.active_score_summary_rows()), [])
        self.assertEqual(
            list(score_services.active_score_summary_rows(target_ids=[self.target.id])),
            [],
        )

    def test_replacement_through_write_path_recomputes_summary(self):
        """Superseded-then-active recompute via the real persistence path."""
        with self.captureOnCommitCallbacks(execute=True):
            score_services.persist_score_observation(
                source=self.source,
                target=self.target,
                score_type=self.type_a,
                value=1.0,
            )
            result = score_services.persist_score_observation(
                source=self.source,
                target=self.target,
                score_type=self.type_a,
                value=5.0,
            )
        self.assertEqual(result.outcome, "changed")
        self.assertEqual(self.target.scores.filter(status=1).count(), 1)
        rows = score_services.active_score_summary_rows(target_ids=[self.target.id])
        # The historical 1.0 must never average in: 5.0, not 3.0.
        self.assertEqual(
            list(rows),
            [{"target_id": self.target.id, "type__name": "Culture", "avg_score": 5.0}],
        )


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "score-cache-versioning-tests",
        }
    }
)
class ScoreCacheVersioningTests(TestCase):
    """Post-deploy rollout regression (issue #461 review, finding 1).

    A deploy swaps the code while the shared cache keeps whatever the
    previous build wrote. The pre-#461 keys held values whose semantics
    changed in #461 (they can average superseded rows), so the versioned key
    builders must make those entries unreachable without any deploy-time
    purge.
    """

    def setUp(self):
        cache.clear()
        self.source = Organization.objects.create(
            name="Source Org", gives_ratings=True
        )
        self.target = Organization.objects.create(name="Target Org")
        self.type_a = ScoreType.objects.create(name="Culture")
        # The exact history #461 is about: a superseded 1.0 replaced by 5.0.
        Score.objects.create(
            source=self.source,
            target=self.target,
            type=self.type_a,
            score=1.0,
            status=Score.INACTIVE_STATUS,
        )
        Score.objects.create(
            source=self.source, target=self.target, type=self.type_a, score=5.0
        )
        # What a pre-#461 deployment cached for this target: the superseded
        # row averaged in ((1.0 + 5.0) / 2 == 3.0).
        self.pre_fix_rows = [{"type__name": "Culture", "avg_score": 3.0}]
        cache.set(f"organization_{self.target.id}_avg_scores", self.pre_fix_rows)

    def test_score_read_keys_carry_the_version_prefix(self):
        version = score_services.SCORE_CACHE_KEY_VERSION
        self.assertEqual(
            score_services.organization_avg_scores_cache_key(7),
            f"{version}:organization_7_avg_scores",
        )
        self.assertEqual(
            score_services.organization_scores_api_cache_key(7),
            f"{version}:organization_scores_api_7",
        )
        self.assertEqual(
            score_services.algorithm_results_cache_key(3),
            f"{version}:algorithm_3_results",
        )
        # The organization-detail response carries attributes only; #461 did
        # not change its values, so pre-deploy entries stay valid and its key
        # is intentionally NOT rotated.
        self.assertEqual(
            score_services.organization_api_cache_key(7), "organization_api_7"
        )

    def test_avg_scores_never_serves_the_pre_fix_entry(self):
        self.assertEqual(
            self.target.avg_scores(),
            [{"type__name": "Culture", "avg_score": 5.0}],
        )

    def test_avg_scores_populates_only_the_versioned_key(self):
        self.target.avg_scores()
        versioned_rows = [{"type__name": "Culture", "avg_score": 5.0}]
        self.assertEqual(
            cache.get(
                score_services.organization_avg_scores_cache_key(self.target.id)
            ),
            versioned_rows,
        )
        # The pre-fix entry is abandoned in place: never read again, it ages
        # out via TTL.
        self.assertEqual(
            cache.get(f"organization_{self.target.id}_avg_scores"),
            self.pre_fix_rows,
        )

    def test_persisting_invalidates_only_the_versioned_key(self):
        versioned_key = score_services.organization_avg_scores_cache_key(
            self.target.id
        )
        cache.set(versioned_key, "stale")
        with self.captureOnCommitCallbacks(execute=True):
            score_services.persist_score_observation(
                source=self.source,
                target=self.target,
                score_type=self.type_a,
                value=5.0,
                provenance={
                    "external_id": "ext-versioning",
                    "source_url": "https://ratings.example.com/org/target",
                    "adapter_version": "v1",
                    "observed_at": "2026-09-14T00:00:00Z",
                    "raw_value": "5.0",
                },
            )
        self.assertIsNone(cache.get(versioned_key))
        # The pre-fix entry still sits under the legacy key, unread.
        self.assertEqual(
            cache.get(f"organization_{self.target.id}_avg_scores"),
            self.pre_fix_rows,
        )


class ScoreTupleAnchorTests(TestCase):
    """The per-tuple anchor row that makes an empty tuple lockable (finding 2).

    MySQL cannot emit the partial unique constraint on Score (W036) and
    select_for_update cannot lock rows that do not exist, so the anchor's
    full unique constraint is the portable guard behind
    ``_persist_locked``'s serialization.
    """

    def setUp(self):
        self.source = Organization.objects.create(
            name="Source Org", gives_ratings=True
        )
        self.target = Organization.objects.create(name="Target Org")
        self.score_type = ScoreType.objects.create(name="Culture")

    def test_get_or_create_is_idempotent_per_tuple(self):
        anchor, created = ScoreTupleAnchor.objects.get_or_create(
            type_id=self.score_type.id,
            source_id=self.source.id,
            target_id=self.target.id,
        )
        self.assertTrue(created)
        same, created_again = ScoreTupleAnchor.objects.get_or_create(
            type_id=self.score_type.id,
            source_id=self.source.id,
            target_id=self.target.id,
        )
        self.assertFalse(created_again)
        self.assertEqual(same.pk, anchor.pk)
        self.assertEqual(ScoreTupleAnchor.objects.count(), 1)

    def test_full_unique_constraint_enforces_one_anchor_per_tuple(self):
        ScoreTupleAnchor.objects.create(
            type_id=self.score_type.id,
            source_id=self.source.id,
            target_id=self.target.id,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ScoreTupleAnchor.objects.create(
                    type_id=self.score_type.id,
                    source_id=self.source.id,
                    target_id=self.target.id,
                )
        self.assertEqual(ScoreTupleAnchor.objects.count(), 1)

    def test_anchor_str_identifies_the_tuple(self):
        anchor = ScoreTupleAnchor.objects.create(
            type_id=self.score_type.id,
            source_id=self.source.id,
            target_id=self.target.id,
        )
        self.assertEqual(
            str(anchor),
            "anchor: {} -> {} [{}]".format(
                self.target.id, self.score_type.id, self.source.id
            ),
        )

    def test_persistence_creates_one_anchor_per_distinct_tuple(self):
        other_target = Organization.objects.create(name="Other Target")
        for target in (self.target, other_target):
            score_services.persist_score_observation(
                source=self.source,
                target=target,
                score_type=self.score_type,
                value=4.0,
                provenance={
                    "external_id": f"ext-{target.id}",
                    "source_url": "https://ratings.example.com/org/target",
                    "adapter_version": "v1",
                    "observed_at": "2026-09-14T00:00:00Z",
                    "raw_value": "4.0",
                },
            )
        self.assertEqual(ScoreTupleAnchor.objects.count(), 2)


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "score-first-write-race-tests",
        }
    }
)
class ScoreFirstWriteRaceTests(TransactionTestCase):
    """Genuine two-connection first-write race for one empty score tuple.

    On MySQL the production race is real: the partial unique constraint on
    Score is not emitted there (W036) and select_for_update cannot lock an
    absent row, so serialization comes from the ScoreTupleAnchor parent row
    -- racing first writers collide on the anchor's full unique constraint
    and then hold select_for_update on it, and the loser reconciles against
    the winner's committed row (exactly one winner, no duplicate active row,
    no error surfaced to either writer).

    SQLite cannot host two concurrent writer transactions (they deadlock at
    the file level, raising "database is locked"; the same documented caveat
    as ConcurrentDoubleSubmitTests), so the default run serializes the same
    two-connection sequence through a harness gate: both writers still use
    genuinely separate connections and the loser's full reconciliation path
    is exercised. To run the genuinely concurrent race against MySQL:

        # one-time, against a disposable MySQL server:
        mysql -e "CREATE DATABASE crank_test CHARACTER SET utf8mb4;"
        SECRET_KEY=test REDIS_MASTER_URL=redis://localhost:6379/0 \\
        DB_NAME=crank_test DB_USER=... DB_PASS=... DB_HOST=127.0.0.1 \\
        python -m pytest crank/tests/services/test_score_persistence.py \\
            --ds crank.settings.mysql_test --create-db -k FirstWriteRace -v
    """

    def setUp(self):
        cache.clear()
        self.source = Organization.objects.create(
            name="Source Org", gives_ratings=True
        )
        self.target = Organization.objects.create(name="Target Org")
        self.score_type = ScoreType.objects.create(name="Race Culture")
        # SQLite needs the harness gate (see docstring); MySQL serializes
        # genuinely through the anchor row lock.
        self.gate = (
            threading.Lock() if connection.vendor != "mysql" else None
        )

    @staticmethod
    def _race_provenance(value):
        return {
            "external_id": "race-461",
            "source_url": "https://ratings.example.com/org/race",
            "adapter_version": "v1",
            "observed_at": "2026-09-14T00:00:00Z",
            "raw_value": str(value),
        }

    def _persist_once(self, value):
        return score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=value,
            provenance=self._race_provenance(value),
        ).outcome

    def _write_once(self, barrier, value):
        """One writer on its own dedicated DB connection."""
        try:
            connections.close_all()
            barrier.wait(timeout=30)
            if self.gate is not None:
                # Two SQLite writer transactions deadlock at the file level;
                # serialize the write region (see class docstring).
                with self.gate:
                    outcome = self._persist_once(value)
            else:
                outcome = self._persist_once(value)
            return ("ok", outcome)
        except Exception as exc:
            return ("error", exc)
        finally:
            connections.close_all()

    def _run_race(self, values):
        barrier = threading.Barrier(2, timeout=30)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self._write_once, barrier, value)
                for value in values
            ]
            return [future.result(timeout=60) for future in futures]

    def _tuple_scores(self):
        return Score.objects.filter(
            type=self.score_type, source=self.source, target=self.target
        )

    def test_two_connection_first_write_race_yields_exactly_one_active(self):
        results = self._run_race([4.5, 4.5])
        # Neither connection may surface an error (the API-layer "no 500"):
        # a losing writer must reconcile, never crash.
        self.assertEqual(
            [status for status, _ in results],
            ["ok", "ok"],
            f"first-write race surfaced an error: {results}",
        )
        outcomes = sorted(payload for _, payload in results)
        # Exactly one winner created the row; the loser reconciled to a noop
        # (identical observation re-read against the winner's committed row).
        self.assertEqual(outcomes, ["created", "noop"])
        # No duplicate active row and no duplicate history row.
        self.assertEqual(self._tuple_scores().count(), 1)
        self.assertEqual(
            self._tuple_scores().filter(status=Score.ACTIVE_STATUS).count(), 1
        )

    @skipUnless(connection.vendor == "mysql", "genuine concurrency needs MySQL")
    def test_mysql_race_with_distinct_values_supersedes_exactly_once(self):
        # Two genuinely concurrent writers with different values: the loser
        # takes over the anchor after the winner commits, supersedes the
        # winner's row exactly once, and leaves a single active row.
        results = self._run_race([3.0, 5.0])
        self.assertEqual(
            [status for status, _ in results],
            ["ok", "ok"],
            f"MySQL first-write race surfaced an error: {results}",
        )
        self.assertEqual(
            sorted(payload for _, payload in results), ["changed", "created"]
        )
        self.assertEqual(self._tuple_scores().count(), 2)
        self.assertEqual(
            self._tuple_scores().filter(status=Score.ACTIVE_STATUS).count(), 1
        )
