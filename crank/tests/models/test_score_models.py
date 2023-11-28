from django.test import TestCase

from crank.models.organization import Organization
from crank.models.score import ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight, Score


class ScoreModelTests(TestCase):
    def test_score_type_creation(self):
        score_type = ScoreType.objects.create(name="Test Score Type")
        self.assertEqual(score_type.name, "Test Score Type")

    def test_score_algorithm_creation(self):
        score_algorithm = ScoreAlgorithm.objects.create(name="Test Score Algorithm")
        self.assertEqual(score_algorithm.name, "Test Score Algorithm")

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
