# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.db.models import Avg, Index, Q, UniqueConstraint

from crank.models.agent_run import AgentRun
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
    # Optional link to the run that produced this observation (run provenance).
    # SET_NULL preserves score history even if a run record is pruned.
    run = models.ForeignKey(
        AgentRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="scores",
        help_text="Run that produced this observation, if any.",
    )
    # Sanitized provenance metadata for the observation (external id, source url,
    # adapter version, observed/fetched timestamps, raw + normalized values).
    # Raw external payloads and secrets are never stored here.
    provenance = models.JSONField(default=dict, blank=True)

    class Meta:
        app_label = 'crank'
        constraints = [
            # this constraint ensures there is only one active score for a given type, source, and target
            UniqueConstraint(name="unique_score_type_source_target_status",
                             fields=["type", "source", "target", "status"],
                             condition=Q(status=1),
                             violation_error_message="There is already an active score of this type")
        ]


class ScoreTupleAnchor(TimeStampedModel):
    """Stable parent row for one (type, source, target) score tuple.

    Serializes concurrent writers for a tuple -- including its *first* write,
    when no score row exists yet for ``select_for_update`` to lock -- because
    MySQL cannot emit the partial unique constraint on ``Score`` (W036) and
    cannot lock absent rows. The full unique constraint below is emitted on
    every backend, so ``get_or_create`` is race-safe (racing creators collide
    on the constraint and the loser re-reads the winner's row with a locking
    read); writers then hold ``select_for_update`` on the anchor for their
    whole transaction, so a loser always reconciles against the winner's
    committed state. Row locks live exactly as long as the transaction, which
    keeps the guarantee even when the service call is nested inside a
    caller's larger atomic block.

    Rows are never deleted on purpose (they anchor one bounded row per scored
    tuple) and cascade with their referenced rows.
    """
    type = models.ForeignKey(ScoreType, on_delete=models.CASCADE,
                             related_name="score_tuple_anchors")
    source = models.ForeignKey(Organization, on_delete=models.CASCADE,
                               related_name="score_tuple_anchors_given")
    target = models.ForeignKey(Organization, on_delete=models.CASCADE,
                               related_name="score_tuple_anchors_received")

    class Meta:
        app_label = 'crank'
        constraints = [
            UniqueConstraint(name="unique_score_tuple_anchor",
                             fields=["type", "source", "target"],
                             violation_error_message="There is already an anchor for this score tuple")
        ]

    def __str__(self):
        return "anchor: {} -> {} [{}]".format(self.target_id, self.type_id, self.source_id)
