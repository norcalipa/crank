# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.db.models import Avg, Index, Q, UniqueConstraint

from crank.models.organization import Organization
from django.db import models
from django_extensions.db.models import TimeStampedModel, ActivatorModel


class ScoreType(TimeStampedModel, ActivatorModel):
    def __str__(self):
        return self.name

    name = models.CharField(max_length=100)

    class Meta:
        app_label = 'crank'


class ScoreAlgorithm(TimeStampedModel, ActivatorModel):
    def __str__(self):
        return self.name

    name = models.CharField(max_length=100)
    description_content = models.CharField(max_length=50, default="")

    class Meta:
        app_label = 'crank'


class ScoreAlgorithmWeight(TimeStampedModel, ActivatorModel):
    type = models.ForeignKey(ScoreType, on_delete=models.CASCADE, limit_choices_to={"status": 1})
    algorithm = models.ForeignKey(ScoreAlgorithm, on_delete=models.CASCADE)
    weight = models.FloatField(default=1.0)

    class Meta:
        app_label = 'crank'
        constraints = [
            # this constraint ensures there is only one active score for a given type, source, and target
            UniqueConstraint(name="unique_type_algorithm_status",
                             fields=["type", "algorithm", "status"],
                             condition=Q(status=1),
                             violation_error_message="There is already an weight for this type")
        ]


class Score(TimeStampedModel, ActivatorModel):
    def __str__(self):
        return "{}: {} [{}]={}".format(self.target.name, self.type.name, self.source.name, self.score)

    type = models.ForeignKey(ScoreType, on_delete=models.CASCADE, default=None,
                             limit_choices_to={"status": 1})
    source = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name="scores_given",
                               limit_choices_to={"status": 1, "gives_ratings": 1})
    target = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name="scores")
    score = models.FloatField(default=0.0)
    low_threshold = models.FloatField(default=0.0)
    high_threshold = models.FloatField(default=5.0)

    class Meta:
        app_label = 'crank'
        constraints = [
            # this constraint ensures there is only one active score for a given type, source, and target
            UniqueConstraint(name="unique_score_type_source_target_status",
                             fields=["type", "source", "target", "status"],
                             condition=Q(status=1),
                             violation_error_message="There is already an active score of this type")
        ]
