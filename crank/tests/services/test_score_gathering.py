# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for bounded score-gathering orchestration."""

from datetime import datetime, timezone
from unittest.mock import patch

from django.test import TestCase, override_settings

from crank.agents.sources.contract import RawScoreObservation, SourceResult
from crank.agents.sources.types import build_resolution_config
from crank.models import AgentRun, Organization, Score, ScoreType
from crank.models.source import ApprovalState, SourceCatalog, SourceRun
from crank.services.score_gathering import (
    COUNT_KEYS,
    ScoreGatheringError,
    gather_scores,
)


class FakeAdapter:
    key = "fake.v1"
    version = "1.0.0"

    def __init__(self, source, observations=(), error=None):
        self.source = source
        self.observations = list(observations)
        self.error = error

    def fetch(self, query):
        if self.error:
            raise self.error
        return SourceResult(observations=self.observations, items_seen=len(self.observations))


def observation(target="Target Org", external_id="ext-1"):
    now = datetime.now(timezone.utc)
    return RawScoreObservation.create(
        external_id=external_id,
        source_url="https://ratings.example.test/score/ext-1",
        target_identity=target,
        score_type="rating",
        value=4.0,
        range_low=0.0,
        range_high=5.0,
        observed_at=now,
        fetched_at=now,
        adapter="fake.v1",
        adapter_version="1.0.0",
    )


@override_settings(
    RATING_SOURCE_ORGANIZATION_NAME="Rating Source",
    SCORE_TYPE_MAPPINGS=[
        {"source": "default", "external": "rating", "score_type": "Culture"}
    ],
    SCORE_TARGET_ALIASES=[],
    GATHER_SCORES_DEADLINE_SECONDS=300,
    GATHER_SCORES_SOURCE_TIMEOUT_SECONDS=120,
)
class ScoreGatheringServiceTests(TestCase):
    def setUp(self):
        self.rating_source = Organization.objects.create(
            name="Rating Source", gives_ratings=True
        )
        self.target = Organization.objects.create(name="Target Org")
        self.score_type = ScoreType.objects.create(name="Culture")
        self.config = build_resolution_config(
            version="test",
            source_key="default",
            source_organization="Rating Source",
            score_type_mappings=[
                {"source": "default", "external": "rating", "score_type": "Culture"}
            ],
        )

    def source(self, name, *, approved=True, enabled=True):
        org = Organization.objects.create(name=f"{name} Rating", gives_ratings=True)
        return SourceCatalog.objects.create(
            organization=org,
            name=name,
            adapter_key="fake.v1",
            base_url="https://ratings.example.test",
            approval_state=(ApprovalState.APPROVED if approved else ApprovalState.PENDING),
            enabled=enabled,
        )

    def make_run(self):
        return AgentRun.objects.create(
            run_type=AgentRun.RunType.GATHER_SCORES,
            status=AgentRun.Status.RUNNING,
        )

    def test_no_sources_returns_zero_counts(self):
        counts = gather_scores(self.make_run(), resolution_config=self.config)
        self.assertEqual(counts["sources_total"], 0)
        self.assertEqual(sum(counts[key] for key in COUNT_KEYS if key != "sources_total"), 0)
        self.assertEqual(SourceRun.objects.count(), 0)

    def test_success_normalizes_and_persists(self):
        source = self.source("good")
        adapter = FakeAdapter(source, [observation()])
        with patch("crank.services.score_gathering.build_adapter", return_value=adapter):
            counts = gather_scores(self.make_run(), resolution_config=self.config)

        self.assertEqual(counts["sources_succeeded"], 1)
        self.assertEqual(counts["sources_failed"], 0)
        self.assertEqual(counts["observations_fetched"], 1)
        self.assertEqual(counts["observations_normalized"], 1)
        self.assertEqual(counts["observations_persisted"], 1)
        self.assertEqual(Score.objects.count(), 1)
        source_run = SourceRun.objects.get(source=source)
        self.assertEqual(source_run.status, AgentRun.Status.SUCCEEDED)

    def test_duplicate_replay_is_counted_and_not_written_twice(self):
        source = self.source("good")
        obs = observation()
        adapter = FakeAdapter(source, [obs, obs])
        with patch("crank.services.score_gathering.build_adapter", return_value=adapter):
            counts = gather_scores(self.make_run(), resolution_config=self.config)
        self.assertEqual(counts["duplicates_skipped"], 1)
        self.assertEqual(Score.objects.count(), 1)

    def test_one_failed_source_does_not_stop_successful_source(self):
        failed = self.source("failed")
        succeeded = self.source("succeeded")
        run = self.make_run()
        adapters = {
            failed.pk: FakeAdapter(failed, error=RuntimeError("upstream unavailable")),
            succeeded.pk: FakeAdapter(succeeded, [observation()]),
        }

        def build(source):
            return adapters[source.pk]

        with patch("crank.services.score_gathering.build_adapter", side_effect=build):
            counts = gather_scores(run, resolution_config=self.config)

        self.assertEqual(counts["sources_total"], 2)
        self.assertEqual(counts["sources_succeeded"], 1)
        self.assertEqual(counts["sources_failed"], 1)
        self.assertEqual(Score.objects.count(), 1)
        self.assertEqual(
            set(SourceRun.objects.values_list("status", flat=True)),
            {AgentRun.Status.SUCCEEDED, AgentRun.Status.FAILED},
        )

    def test_all_sources_failed_raises_with_aggregate_counts(self):
        source = self.source("failed")
        adapter = FakeAdapter(source, error=RuntimeError("upstream unavailable"))
        with patch("crank.services.score_gathering.build_adapter", return_value=adapter):
            with self.assertRaises(ScoreGatheringError) as raised:
                gather_scores(self.make_run(), resolution_config=self.config)
        self.assertEqual(raised.exception.counts["sources_failed"], 1)
        self.assertEqual(SourceRun.objects.get().status, AgentRun.Status.FAILED)

    def test_deadline_stops_remaining_sources(self):
        first = self.source("first")
        second = self.source("second")
        run = self.make_run()
        adapter = FakeAdapter(first, [observation()])
        with patch("crank.services.score_gathering.build_adapter", return_value=adapter), patch(
            "crank.services.score_gathering.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 2.0, 2.0],
        ):
            with self.assertRaises(ScoreGatheringError) as raised:
                gather_scores(
                    run,
                    resolution_config=self.config,
                    deadline_seconds=1,
                )
        self.assertEqual(raised.exception.counts["sources_succeeded"], 0)
        self.assertEqual(raised.exception.counts["sources_failed"], 2)
        self.assertEqual(SourceRun.objects.count(), 2)

    def test_query_from_options_dict(self):
        from crank.agents.sources.contract import SourceQuery
        from crank.services.score_gathering import _query_from_options
        q = _query_from_options({"query": {"term": "cafe"}}, 100)
        self.assertIsInstance(q, SourceQuery)
        self.assertEqual(q.term, "cafe")
        self.assertEqual(q.max_observations, 100)

    def test_query_from_options_invalid_type(self):
        from crank.services.score_gathering import _query_from_options
        with self.assertRaises(TypeError):
            _query_from_options({"query": 42}, 100)

    def test_result_observations_plain_list(self):
        from crank.services.score_gathering import _result_observations
        result = [1, 2, 3]
        self.assertEqual(_result_observations(result), [1, 2, 3])

    def test_source_timeout_raises(self):
        source = self.source("slow")
        run = self.make_run()
        def slow_build_and_fetch(src, query):
            import time as t
            t.sleep(0.2)
            return FakeAdapter(src), FakeAdapter(src).fetch(query)
        with patch("crank.services.score_gathering._build_and_fetch", side_effect=slow_build_and_fetch):
            with self.assertRaises(ScoreGatheringError):
                gather_scores(run, resolution_config=self.config, source_timeout_seconds=0.01)


    def test_query_from_options_sourcequery_passthrough(self):
        from crank.agents.sources.contract import SourceQuery
        from crank.services.score_gathering import _query_from_options
        q = SourceQuery(term="test")
        result = _query_from_options({"query": q}, 100)
        self.assertIs(result, q)

    def test_normalizer_shaped_observation_passthrough(self):
        from crank.services.score_gathering import _as_normalizer_observation

        class FakeNorm:
            external_type = "rating"
            target = "Target Org"

        result = _as_normalizer_observation(
            FakeNorm(), source=None, config=None, run=None
        )
        self.assertEqual(result.external_type, "rating")
