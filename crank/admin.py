# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.contrib import admin
from crank.models.agent_run import AgentRun
from crank.models.conversation import Conversation, Message
from crank.models.organization import Organization
from crank.models.preference import UserPreference, UserPreferenceAudit
from crank.models.score import Score, ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight
from crank.models.source import ApprovalState, SourceCatalog, SourceRun, SourceCatalogAudit


class StaffOnlyAdminMixin:
    """Restrict admin access to staff users.

    Django's admin site already requires ``is_staff`` to reach these views;
    this mixin makes the authorization explicit and, for non-staff users,
    returns an empty queryset so that sensitive profile/preference data stays
    staff-only without raising in code paths that call ``get_queryset``
    outside of a permission check (e.g. admin actions, bulk operations).
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
            # Django admin convention: return an empty queryset rather than
            # raising PermissionDenied, so any code path (admin actions,
            # custom views) that reaches get_queryset without a permission
            # gate gets an empty result set instead of a 500.
            return super().get_queryset(request).none()
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


class UserPreferenceAuditAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = UserPreferenceAudit
    list_display = ['user', 'action', 'schema_version', 'change_count', 'created']
    list_select_related = ['user']
    list_filter = ['action', 'schema_version']
    search_fields = ['user__username']
    readonly_fields = ['user', 'action', 'schema_version', 'change_count', 'created']


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


class AgentRunAdmin(admin.ModelAdmin):
    model = AgentRun
    list_display = ['run_type', 'status', 'started_at', 'finished_at', 'correlation_id']
    list_filter = ['status', 'run_type']
    search_fields = ['correlation_id', 'error_summary']
    readonly_fields = ['correlation_id', 'created', 'modified']


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(ScoreType, ScoreTypeAdmin)
admin.site.register(ScoreAlgorithm, ScoreAlgorithmAdmin)
admin.site.register(ScoreAlgorithmWeight)
admin.site.register(Score, ScoreAdmin)
admin.site.register(UserPreference, UserPreferenceAdmin)
admin.site.register(UserPreferenceAudit, UserPreferenceAuditAdmin)
admin.site.register(Conversation, ConversationAdmin)
admin.site.register(Message, MessageAdmin)


class SourceCatalogAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    """Source-catalog admin that records auditable, credentials-safe changes."""

    model = SourceCatalog
    list_display = [
        "name",
        "organization",
        "adapter_key",
        "approval_state",
        "enabled",
        "cadence",
        "last_success_at",
        "last_failure_at",
    ]
    list_filter = ["approval_state", "enabled", "cadence"]
    list_editable = ["enabled"]
    list_select_related = ["organization"]
    search_fields = ["name", "organization__name", "adapter_key"]
    readonly_fields = ["approved_at", "created", "modified"]
    filter_horizontal = ["supported_score_types"]
    actions = ["approve_sources", "block_sources", "enable_sources", "disable_sources"]

    def save_model(self, request, obj, form, change):
        """Persist the row and record an audit (created/changed deltas)."""
        old = None
        if change:
            try:
                old = SourceCatalog.objects.get(pk=obj.pk)
            except SourceCatalog.DoesNotExist:
                old = None
        super().save_model(request, obj, form, change)
        if not change:
            SourceCatalogAudit.record(
                source=obj, user=request.user, action=SourceCatalogAudit.Action.CREATED,
                note=f"Source catalog created via admin.",
            )
            return
        changes = {}
        if old is not None:
            for field_name in ("name", "adapter_key", "base_url", "approval_state",
                               "enabled", "cadence", "timeout_seconds",
                               "rate_limit_per_minute", "max_response_bytes"):
                new_v = getattr(obj, field_name)
                old_v = getattr(old, field_name)
                if new_v != old_v:
                    changes[field_name] = (old_v, new_v)
        if changes:
            SourceCatalogAudit.record(
                source=obj, user=request.user, action=SourceCatalogAudit.Action.CHANGED,
                changes=changes, note=f"Source catalog updated via admin.",
            )

    def _record_state_action(self, request, queryset, action):
        from django.utils import timezone as dj_tz
        updated = 0
        for src in queryset:
            if action == "approve":
                src.approval_state = ApprovalState.APPROVED
                src.approved_at = dj_tz.now()
            elif action == "block":
                src.approval_state = ApprovalState.BLOCKED
            if action in ("enable",):
                src.enabled = True
            if action in ("disable",):
                src.enabled = False
            src.save(update_fields=["approval_state", "approved_at", "enabled"])
            audit_action = {
                "approve": SourceCatalogAudit.Action.APPROVED,
                "block": SourceCatalogAudit.Action.BLOCKED,
                "enable": SourceCatalogAudit.Action.ENABLED,
                "disable": SourceCatalogAudit.Action.DISABLED,
            }[action]
            SourceCatalogAudit.record(
                source=src, user=request.user, action=audit_action,
                note=f"Source {action}d via admin action.",
            )
            updated += 1
        self.message_user(request, f"{updated} source(s) {action}d and audited.")

    @admin.action(description="Approve selected sources")
    def approve_sources(self, request, queryset):
        self._record_state_action(request, queryset, "approve")

    @admin.action(description="Block selected sources")
    def block_sources(self, request, queryset):
        self._record_state_action(request, queryset, "block")

    @admin.action(description="Enable selected sources")
    def enable_sources(self, request, queryset):
        self._record_state_action(request, queryset, "enable")

    @admin.action(description="Disable selected sources")
    def disable_sources(self, request, queryset):
        self._record_state_action(request, queryset, "disable")


class SourceRunAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = SourceRun
    list_display = [
        "source", "status", "adapter_version", "started_at", "finished_at",
    ]
    list_filter = ["status"]
    list_select_related = ["source"]
    search_fields = ["source__name", "error_summary"]
    readonly_fields = ["source", "agent_run", "status", "adapter_version",
                       "counts", "error_summary", "created", "modified"]


class SourceCatalogAuditAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = SourceCatalogAudit
    list_display = ["source", "user", "action", "created"]
    list_filter = ["action"]
    list_select_related = ["source", "user"]
    search_fields = ["source__name"]
    readonly_fields = ["source", "user", "action", "changed_fields", "note", "created"]


admin.site.register(SourceCatalog, SourceCatalogAdmin)
admin.site.register(SourceRun, SourceRunAdmin)
admin.site.register(SourceCatalogAudit, SourceCatalogAuditAdmin)
