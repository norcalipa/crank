from django.contrib import admin
from crank.models.organization import Organization
from crank.models.score import Score, ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight


class ScoreInline(admin.TabularInline):
    model = Score
    fk_name = 'target'
    fields = ['status', 'type', 'source', 'score']


class OrganizationAdmin(admin.ModelAdmin):
    model = Organization

    inlines = [ScoreInline]


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(ScoreType)
admin.site.register(ScoreAlgorithm)
admin.site.register(ScoreAlgorithmWeight)
admin.site.register(Score)
