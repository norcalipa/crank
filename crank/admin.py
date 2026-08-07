# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from crank.models.conversation import Conversation, Message
from crank.models.organization import Organization
from crank.models.preference import UserPreference
from crank.models.score import Score, ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight


class StaffOnlyAdminMixin:
    """Restrict admin access to staff users.

    Django's admin site already requires ``is_staff`` to reach these views;
    this mixin makes the authorization explicit and forwards non-staff users
    to a 403 so that sensitive profile/preference data stays staff-only.
    """

    def has_module_permission(self, request):
        return bool(request.user and request.user.is_staff)

    def has_view_permission(self, request, obj=None):
        return bool(request.user and request.user.is_staff)

    def has_add_permission(self, request):
        return bool(request.user and request.user.is_staff)

    def has_change_permission(self, request, obj=None):
        return bool(request.user and request.user.is_staff)

    def has_delete_permission(self, request, obj=None):
        return bool(request.user and request.user.is_staff)

    def get_queryset(self, request):
        if not request.user.is_staff:
            raise PermissionDenied
        return super().get_queryset(request)


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
    list_filter = ['status', 'type', 'gives_ratings']
    list_editable = ['type', 'funding_round', 'rto_policy']
    search_fields = ['name']
    model = Organization
    inlines = [ScoreInline]


class UserPreferenceAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = UserPreference
    list_display = ["user", "schema_version", "created", "modified"]
    list_select_related = ["user"]
    search_fields = ["user__username", "user__email"]
    readonly_fields = [
        "user",
        "preferences",
        "preferences_markdown",
        "schema_version",
        "created",
        "modified",
    ]


class MessageInline(admin.TabularInline):
    model = Message
    fk_name = "conversation"
    extra = 0
    # Keep sensitive message content out of list/inline views; it is only
    # visible as a read-only field on the individual change form.
    fields = ["role", "status", "order", "content", "created", "modified"]
    readonly_fields = ["content", "created", "modified"]


class ConversationAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = Conversation
    list_display = ["id", "user", "title", "status", "retention_until", "created", "modified"]
    list_filter = ["status"]
    search_fields = ["user__username", "user__email", "title"]
    list_select_related = ["user"]
    readonly_fields = ["created", "modified"]
    inlines = [MessageInline]


class MessageAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = Message
    # Avoid exposing message content in the list view.
    list_display = ["id", "conversation", "role", "order", "status", "created"]
    list_select_related = ["conversation__user"]
    search_fields = ["conversation__user__username", "conversation__user__email"]
    readonly_fields = ["conversation", "role", "content", "order", "created", "modified"]


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(ScoreType, ScoreTypeAdmin)
admin.site.register(ScoreAlgorithm, ScoreAlgorithmAdmin)
admin.site.register(ScoreAlgorithmWeight)
admin.site.register(Score, ScoreAdmin)
admin.site.register(UserPreference, UserPreferenceAdmin)
admin.site.register(Conversation, ConversationAdmin)
admin.site.register(Message, MessageAdmin)
