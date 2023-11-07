from django.contrib import admin
from crank.models.organization import Organization
from crank.models.score import Score, ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight

admin.site.register(Organization)
admin.site.register(ScoreType)
admin.site.register(ScoreAlgorithm)
admin.site.register(ScoreAlgorithmWeight)
admin.site.register(Score)
