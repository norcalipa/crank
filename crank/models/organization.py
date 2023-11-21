from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel, ActivatorModel
from django.db.models import TextChoices, Avg


class Organization(TimeStampedModel, ActivatorModel):

    def __str__(self):
        return self.name

    class Type(TextChoices):
        COMPANY = "C", _("Company (for profit)")
        NON_PROFIT = "N", _("Non-Profit Organization")

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
    type = models.CharField(max_length=1,
                            default=Type.COMPANY,
                            choices=Type.choices)
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

    def avg_scores(self):
        results = self.scores.values("type__name").annotate(avg_score=Avg('score'))
        return results

