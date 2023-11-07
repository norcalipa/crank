from django.db.models import Avg

from crank.models.organization import Organization
from django.db import models
from django_extensions.db.models import TimeStampedModel, ActivatorModel


class ScoreType(TimeStampedModel, ActivatorModel):
    def __str__(self):
        return self.name

    name = models.CharField(max_length=100)


class ScoreAlgorithm(TimeStampedModel, ActivatorModel):
    def __str__(self):
        return self.name

    name = models.CharField(max_length=100)
    description_content = models.CharField(max_length=50, default="")


class ScoreAlgorithmWeight(TimeStampedModel, ActivatorModel):
    type = models.ForeignKey(ScoreType, on_delete=models.CASCADE)
    algorithm = models.ForeignKey(ScoreAlgorithm, on_delete=models.CASCADE)
    weight = models.FloatField(default=1.0)


class Score(TimeStampedModel, ActivatorModel):
    def __str__(self):
        return "{}: {} [{}]={} ".format(self.target.name, self.type.name, self.source.name, self.score)

    type = models.ForeignKey(ScoreType, on_delete=models.CASCADE, default=None)
    source = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name="scores_given")
    target = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name="scores")
    score = models.FloatField(default=0.0)
    low_threshold = models.FloatField(default=0.0)
    high_threshold = models.FloatField(default=5.0)
