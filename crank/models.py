
from django.db import models


class OrganizationType(models.Model):
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


class Organization(models.Model):
    def __str__(self):
        return self.name

    name = models.CharField(max_length=100, unique=True)
    type = models.ForeignKey(OrganizationType, on_delete=models.RESTRICT)
    url = models.URLField(max_length=200, default="")
    gives_ratings = models.BooleanField(default=False)


class ScoreType(models.Model):
    def __str__(self):
        return self.name

    name = models.CharField(max_length=100)


class ScoreAlgorithm(models.Model):
    def __str__(self):
        return self.name

    name = models.CharField(max_length=100)
    description_content = models.CharField(max_length=50, default="")


class ScoreAlgorithmWeights(models.Model):
    type = models.ForeignKey(ScoreType, on_delete=models.CASCADE)
    algorithm = models.ForeignKey(ScoreAlgorithm, on_delete=models.CASCADE)
    weight = models.FloatField(default=1.0)


class Score(models.Model):
    def __str__(self):
        return str(self.score)

    source = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name="source_organization")
    target = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name="target_organization")
    score = models.FloatField(default=0.0)
    low_threshold = models.FloatField(default=0.0)
    high_threshold = models.FloatField(default=5.0)
