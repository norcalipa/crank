from django.contrib import admin
from crank.models.organization import Organization
from crank.models.score import Score, ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight


# This function disables the inline icons for adding, changing, and deleting related objects.
def disable_inline_icons(formset, fieldname):
    formset.form.base_fields[fieldname].widget.can_view_related = False
    formset.form.base_fields[fieldname].widget.can_add_related = False
    formset.form.base_fields[fieldname].widget.can_change_related = False
    formset.form.base_fields[fieldname].widget.can_delete_related = False


class ScoreInline(admin.TabularInline):
    model = Score
    fk_name = 'target'
    fields = ['status', 'type', 'source', 'score']
    extra = 1

    def get_formset(self, request, obj=None, **kwargs):
        fs = super().get_formset(request, obj, **kwargs)
        disable_inline_icons(fs, 'type')
        disable_inline_icons(fs, 'source')
        return fs


class ScoreAlgorithmWeightInline(admin.TabularInline):
    model = ScoreAlgorithmWeight
    fk_name = 'algorithm'
    fields = ['status', 'type', 'weight']
    extra = 1

    def get_formset(self, request, obj=None, **kwargs):
        fs = super().get_formset(request, obj, **kwargs)
        disable_inline_icons(fs, 'type')
        return fs


class ScoreAdmin(admin.ModelAdmin):
    model = Score
    list_display = ['target', 'type', 'score', 'source']
    list_editable = ['score', 'type', 'source']
    list_filter = ['status', 'type']
    search_fields = ['target__name']
    list_select_related = ['type', 'source']


class ScoreTypeAdmin(admin.ModelAdmin):
    model = ScoreType
    list_display = ['status', 'name']
    list_filter = ['status']


class ScoreAlgorithmAdmin(admin.ModelAdmin):
    model = ScoreAlgorithm
    list_display = ['name', 'description_content']
    list_filter = ['status']
    inlines = [ScoreAlgorithmWeightInline]


class OrganizationAdmin(admin.ModelAdmin):
    list_display = ['name', 'type', 'url', 'gives_ratings', 'public', 'funding_round', 'rto_policy']
    list_filter = ['status', 'type']
    list_editable = ['type', 'funding_round', 'rto_policy']
    search_fields = ['name']
    model = Organization
    inlines = [ScoreInline]


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(ScoreType, ScoreTypeAdmin)
admin.site.register(ScoreAlgorithm, ScoreAlgorithmAdmin)
admin.site.register(ScoreAlgorithmWeight)
admin.site.register(Score, ScoreAdmin)
