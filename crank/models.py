
from django.db import models
from django.db.models import Avg, TextChoices
# TimeStampedModel - adds created and updated timestamps to the model
# ActivatorModel - adds active/inactive status to the model
from django_extensions.db.models import TimeStampedModel, ActivatorModel
from django.utils.translation import gettext_lazy as _


class OrganizationType(TimeStampedModel, ActivatorModel):
    COMPANY = "C"
    NON_PROFIT = "N"
    TYPE = (
        (COMPANY, "Company (for profit)"),
        (NON_PROFIT, "Non-Profit Organization"),
    )

    def __str__(self):
        return self.name

    short_name = models.CharField(max_length=1, default=COMPANY)
    name = models.CharField(max_length=20)


class Organization(TimeStampedModel, ActivatorModel):
    def __str__(self):
        return self.name

    class FundingRound(TextChoices):
        SEED = "S", _("Seed")
        SERIES_A = "A", _("Series A")
        SERIES_B = "B", _("Series B")
        SERIES_C = "C", _("Series C")
        SERIES_D = "D", _("Series D")
        SERIES_E = "E", _("Series E")
        SERIES_F = "F", _("Series F")
        SERIES_X = "X", _("Series G or Later")
        SERIES_O = "O", _("Other Private")
        PUBLIC = "P", _("Public")

    class RTOPolicy(TextChoices):
        REMOTE = "R", _("Remote")
        HYBRID = "H", _("Hybrid")
        IN_OFFICE = "O", _("In-Office")

    name = models.CharField(max_length=100, unique=True)
    type = models.ForeignKey(OrganizationType, on_delete=models.RESTRICT)
    url = models.URLField(max_length=200, default="")
    gives_ratings = models.BooleanField(default=False)
    public = models.BooleanField(default=True)
    funding_round = models.CharField(
        max_length=1,
        default=FundingRound.PUBLIC,
        choices=FundingRound.choices,
    )
    rto_policy = models.CharField(
        max_length=1,
        default=RTOPolicy.HYBRID,
        choices=RTOPolicy.choices,
    )


    def average_scores(self):
        return self.scores_received.values("type__name").annotate(avg_score=Avg("score"))

    @property
    def overall_score(self):
        all_scores = self.average_scores()
        return all_scores.aggregate(Avg("avg_score"))["avg_score__avg"]


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
    target = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name="scores_received")
    score = models.FloatField(default=0.0)
    low_threshold = models.FloatField(default=0.0)
    high_threshold = models.FloatField(default=5.0)