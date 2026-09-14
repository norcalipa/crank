# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.core.cache import cache
from django.test import TestCase, override_settings

from crank.models.organization import Organization
from crank.models.score import ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight, Score


class ScoreModelTests(TestCase):
    def test_score_type_creation(self):
        score_type = ScoreType.objects.create(name="Test Score Type")
        self.assertEqual(score_type.name, "Test Score Type")
        self.assertEqual(str(score_type), "Test Score Type")

    def test_score_algorithm_creation(self):
        score_algorithm = ScoreAlgorithm.objects.create(name="Test Score Algorithm")
        self.assertEqual(score_algorithm.name, "Test Score Algorithm")
        self.assertEqual(str(score_algorithm), "Test Score Algorithm")

    def test_score_algorithm_weight_creation(self):
        score_type = ScoreType.objects.create(name="Test Score Type")
        score_algorithm = ScoreAlgorithm.objects.create(name="Test Score Algorithm")
        score_algorithm_weight = ScoreAlgorithmWeight.objects.create(type=score_type, algorithm=score_algorithm,
                                                                     weight=2.0)
        self.assertEqual(score_algorithm_weight.type, score_type)
        self.assertEqual(score_algorithm_weight.algorithm, score_algorithm)
        self.assertEqual(score_algorithm_weight.weight, 2.0)

    def test_score_creation(self):
        score_type = ScoreType.objects.create(name="Test Score Type")
        source_org = Organization.objects.create(name="Source Organization")
        target_org = Organization.objects.create(name="Target Organization")
        score = Score.objects.create(type=score_type, source=source_org, target=target_org, score=3.0)
        self.assertEqual(score.type, score_type)
        self.assertEqual(score.source, source_org)
        self.assertEqual(score.target, target_org)
        self.assertEqual(score.score, 3.0)
        self.assertEqual(str(score), "Target Organization: Test Score Type [Source Organization]=3.0")


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "avg-scores-tests",
        }
    }
)
class AvgScoresTests(TestCase):
    """Organization.avg_scores must aggregate active scores of active types
    only (issue #461): superseded rows and deactivated types never leak in."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_avg_scores_excludes_superseded_rows_and_inactive_types(self):
        source = Organization.objects.create(name="Source Org", gives_ratings=True)
        target = Organization.objects.create(name="Target Org")
        active_type = ScoreType.objects.create(name="Culture")
        retired_type = ScoreType.objects.create(name="Legacy", status=0)
        Score.objects.create(source=source, target=target, type=active_type, score=1.0, status=0)
        Score.objects.create(source=source, target=target, type=active_type, score=5.0)
        Score.objects.create(source=source, target=target, type=retired_type, score=4.0)

        self.assertEqual(
            target.avg_scores(),
            [{"type__name": "Culture", "avg_score": 5.0}],
        )

    def test_avg_scores_averages_multiple_active_rows(self):
        source = Organization.objects.create(name="Source Org 2", gives_ratings=True)
        other_source = Organization.objects.create(
            name="Second Source Org 2", gives_ratings=True
        )
        target = Organization.objects.create(name="Target Org 2")
        active_type = ScoreType.objects.create(name="Culture")
        # Distinct sources: the partial unique constraint allows one active
        # row per (type, source, target).
        Score.objects.create(source=source, target=target, type=active_type, score=4.0)
        Score.objects.create(source=other_source, target=target, type=active_type, score=6.0)

        self.assertEqual(
            target.avg_scores(),
            [{"type__name": "Culture", "avg_score": 5.0}],
        )

    def test_avg_scores_empty_score_set(self):
        org = Organization.objects.create(name="No Score Org")
        self.assertEqual(org.avg_scores(), [])

    def test_avg_scores_serves_active_only_summary_from_cache(self):
        source = Organization.objects.create(name="Source Org 3", gives_ratings=True)
        target = Organization.objects.create(name="Target Org 3")
        active_type = ScoreType.objects.create(name="Culture")
        Score.objects.create(source=source, target=target, type=active_type, score=5.0)

        first = target.avg_scores()
        # Second read hits the same cache key without changing shape.
        self.assertEqual(target.avg_scores(), first)
        self.assertEqual(first, [{"type__name": "Culture", "avg_score": 5.0}])
