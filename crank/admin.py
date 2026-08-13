# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.contrib import admin
from django.db import models
from crank.models.agent_run import AgentRun
from crank.models.crawl_run import CrawlRun
from crank.models.conversation import Conversation, Message
from crank.models.employer import EmployerAlias, UnresolvedEmployer
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.organization import Organization
from crank.models.company_profile import CompanyProfileObservation
from crank.models.company_request import CompanyRequest
from crank.models.preference import UserPreference, UserPreferenceAudit
from crank.models.score import Score, ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight
from crank.models.source import ApprovalState, SourceCatalog, SourceRun, SourceCatalogAudit
from crank.models.monitoring import CapabilitySwitch, OperationalChangeAudit
from crank.services import monitoring
from crank.services.crawl_runs import CrawlRequestError, trigger_crawl


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


class AgentRunAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = AgentRun
    list_display = ['run_type', 'status', 'started_at', 'finished_at', 'correlation_id']
    list_filter = ['status', 'run_type']
    search_fields = ['correlation_id', 'error_summary']
    readonly_fields = [
        'correlation_id', 'created', 'modified', 'started_at', 'finished_at',
        'counts', 'error_summary',
    ]


class CompanyProfileObservationAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = CompanyProfileObservation
    list_display = [
        "observed_name", "observed_domain", "organization", "status",
        "observed_at", "extraction_version",
    ]
    list_filter = ["status", "extraction_version"]
    list_select_related = ["organization", "reviewed_by"]
    search_fields = ["observed_name", "observed_domain", "source_url"]
    readonly_fields = [
        "organization", "source_url", "observed_domain", "observed_name",
        "description", "locations", "rto_evidence", "funding_evidence",
        "public_status_evidence", "logo_url", "brand_metadata", "observed_at",
        "extraction_version", "conflict_fields", "fingerprint", "created",
        "modified", "reviewed_by", "reviewed_at",
    ]
    actions = ["accept_observations", "reject_observations", "conflict_observations"]

    def _review(self, request, queryset, status):
        count = 0
        for observation in queryset:
            observation.mark_reviewed(status=status, user=request.user)
            count += 1
        self.message_user(request, f"{count} company profile observation(s) marked {status}.")

    @admin.action(description="Accept selected company profile observations")
    def accept_observations(self, request, queryset):
        self._review(request, queryset, CompanyProfileObservation.Status.ACCEPTED)

    @admin.action(description="Reject selected company profile observations")
    def reject_observations(self, request, queryset):
        self._review(request, queryset, CompanyProfileObservation.Status.REJECTED)

    @admin.action(description="Mark selected observations conflicted")
    def conflict_observations(self, request, queryset):
        self._review(request, queryset, CompanyProfileObservation.Status.CONFLICTED)


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(CompanyProfileObservation, CompanyProfileObservationAdmin)
class CompanyRequestAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = CompanyRequest
    list_display = ["company_name", "website_url", "requester", "status", "created", "crawl_source_approved", "refresh_queued"]
    list_filter = ["status", "crawl_source_approved", "refresh_queued"]
    search_fields = ["company_name", "normalized_domain", "requester__username", "requester__email"]
    list_select_related = ["requester", "duplicate_of", "approved_organization"]
    readonly_fields = ["requester", "company_name", "normalized_name", "website_url", "normalized_domain", "careers_url", "reason", "status", "duplicate_of", "approved_organization", "created", "modified", "crawl_source_approved", "refresh_queued"]
    actions = ["approve_requests", "reject_requests", "mark_duplicate", "approve_crawl_sources", "queue_refresh"]

    def _confirmed(self, request):
        if request.POST.get("confirm") != "yes":
            self.message_user(request, "No changes made. Repeat the action with confirm=yes.", level="warning")
            return False
        return True

    def _audit(self, request, item, action, old, new):
        OperationalChangeAudit.record(actor=request.user, target_type="company_request", target_id=item.pk, action=action, old_value=old, new_value=new, confirmed=True)

    @admin.action(description="Approve and create pending organizations (confirm=yes)")
    def approve_requests(self, request, queryset):
        if not self._confirmed(request):
            return
        updated = 0
        for item in queryset.filter(status=CompanyRequest.Status.PENDING):
            organization = Organization.objects.create(name=item.company_name, url=item.website_url, status=0, public=True)
            old = {"status": item.status, "approved_organization": None}
            item.status = CompanyRequest.Status.APPROVED
            item.approved_organization = organization
            item.save(update_fields=["status", "approved_organization", "modified"])
            self._audit(request, item, "approve", old, {"status": item.status, "approved_organization": organization.pk})
            updated += 1
        self.message_user(request, f"{updated} suggestion(s) approved as pending organizations.")

    @admin.action(description="Reject selected suggestions (confirm=yes)")
    def reject_requests(self, request, queryset):
        if not self._confirmed(request):
            return
        updated = 0
        for item in queryset.filter(status=CompanyRequest.Status.PENDING):
            old = {"status": item.status}
            item.status = CompanyRequest.Status.REJECTED
            item.save(update_fields=["status", "modified"])
            self._audit(request, item, "reject", old, {"status": item.status})
            updated += 1
        self.message_user(request, f"{updated} suggestion(s) rejected and audited.")

    @admin.action(description="Mark selected suggestions as duplicates (confirm=yes)")
    def mark_duplicate(self, request, queryset):
        if not self._confirmed(request):
            return
        updated = 0
        for item in queryset.filter(status=CompanyRequest.Status.PENDING).exclude(duplicate_of=None):
            old = {"status": item.status, "duplicate_of": None}
            item.status = CompanyRequest.Status.DUPLICATE
            item.save(update_fields=["status", "modified"])
            self._audit(request, item, "duplicate", old, {"status": item.status, "duplicate_of": item.duplicate_of_id})
            updated += 1
        self.message_user(request, f"{updated} suggestion(s) marked as duplicates and audited.")

    @admin.action(description="Approve crawl source workflow (confirm=yes)")
    def approve_crawl_sources(self, request, queryset):
        if not self._confirmed(request):
            return
        updated = 0
        for item in queryset.filter(status=CompanyRequest.Status.APPROVED, crawl_source_approved=False):
            item.crawl_source_approved = True
            item.save(update_fields=["crawl_source_approved", "modified"])
            self._audit(request, item, "approve_source", {"crawl_source_approved": False}, {"crawl_source_approved": True})
            updated += 1
        self.message_user(request, f"{updated} crawl source workflow(s) approved; no external calls were made.")

    @admin.action(description="Queue refresh after source approval (confirm=yes)")
    def queue_refresh(self, request, queryset):
        if not self._confirmed(request):
            return
        updated = 0
        for item in queryset.filter(status=CompanyRequest.Status.APPROVED, crawl_source_approved=True, refresh_queued=False):
            item.refresh_queued = True
            item.save(update_fields=["refresh_queued", "modified"])
            self._audit(request, item, "queue_refresh", {"refresh_queued": False}, {"refresh_queued": True})
            updated += 1
        self.message_user(request, f"{updated} refresh request(s) queued for the approved workflow.")


admin.site.register(CompanyRequest, CompanyRequestAdmin)

admin.site.register(ScoreType, ScoreTypeAdmin)
admin.site.register(ScoreAlgorithm, ScoreAlgorithmAdmin)
admin.site.register(ScoreAlgorithmWeight)
admin.site.register(Score, ScoreAdmin)
admin.site.register(UserPreference, UserPreferenceAdmin)
admin.site.register(UserPreferenceAudit, UserPreferenceAuditAdmin)
admin.site.register(Conversation, ConversationAdmin)
admin.site.register(Message, MessageAdmin)
admin.site.register(AgentRun, AgentRunAdmin)


class CrawlRunSourceInline(admin.TabularInline):
    model = CrawlRun
    fk_name = "source"
    extra = 0
    can_delete = False
    fields = ["source_key", "outcome", "requested_by", "started_at", "finished_at", "counts", "error_summary"]
    readonly_fields = fields
    ordering = ["-started_at", "-id"]


class CrawlRunJobSourceInline(admin.TabularInline):
    model = CrawlRun
    fk_name = "job_source"
    extra = 0
    can_delete = False
    fields = ["source_key", "outcome", "requested_by", "started_at", "finished_at", "counts", "error_summary"]
    readonly_fields = fields
    ordering = ["-started_at", "-id"]


class SourceCatalogAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    """Source-catalog admin that records auditable, credentials-safe changes."""

    model = SourceCatalog
    inlines = [CrawlRunSourceInline]
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
    actions = ["approve_sources", "block_sources", "enable_sources", "disable_sources", "trigger_crawls"]

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
            OperationalChangeAudit.record(
                actor=request.user,
                target_type="rating_source",
                target_id=obj.pk,
                action="changed",
                old_value={field: old for field, (old, _new) in changes.items()},
                new_value={field: new for field, (_old, new) in changes.items()},
                confirmed=True,
            )

    def _record_state_action(self, request, queryset, action):
        from django.utils import timezone as dj_tz
        post = getattr(request, "POST", None)
        if post is not None and post.get("confirm") != "yes":
            self.message_user(
                request,
                "No changes made. Repeat the action with confirm=yes.",
                level="warning",
            )
            return
        updated = 0
        for src in queryset:
            old = {"approval_state": src.approval_state, "enabled": src.enabled}
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
                changes={
                    "approval_state": (old["approval_state"], src.approval_state),
                    "enabled": (old["enabled"], src.enabled),
                },
                note=f"Source {action}d via admin action; confirmed=yes.",
            )
            OperationalChangeAudit.record(
                actor=request.user,
                target_type="rating_source",
                target_id=src.pk,
                action=action,
                old_value=old,
                new_value={"approval_state": src.approval_state, "enabled": src.enabled},
                confirmed=True,
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

    @admin.action(description="Trigger crawl for selected sources (confirm=yes)")
    def trigger_crawls(self, request, queryset):
        if getattr(request, "POST", {}).get("confirm") != "yes":
            self.message_user(request, "No crawls started. Repeat the action with confirm=yes.", level="warning")
            return
        started = 0
        for source in queryset:
            try:
                trigger_crawl(source_key=source.adapter_key, source_type="organization", requested_by=request.user)
                started += 1
            except CrawlRequestError as exc:
                self.message_user(request, f"{source.name}: {exc}", level="error")
        self.message_user(request, f"{started} crawl(s) triggered and audited.")


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


class JobSourceCatalogAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    """Staff-only policy diagnosis without credentials or raw responses."""

    model = JobSourceCatalog
    inlines = [CrawlRunJobSourceInline]
    list_display = ["name", "adapter_key", "approval_state", "enabled", "base_url", "created", "modified"]
    list_filter = ["approval_state", "enabled"]
    search_fields = ["name", "adapter_key", "base_url"]
    readonly_fields = ["created", "modified"]
    actions = ["enable_sources", "disable_sources", "approve_sources", "block_sources", "trigger_crawls"]

    def _state_action(self, request, queryset, action):
        """Apply an operational state change only after explicit confirmation."""
        post = getattr(request, "POST", None)
        if post is not None and post.get("confirm") != "yes":
            self.message_user(
                request,
                "No changes made. Repeat the action with confirm=yes.",
                level="warning",
            )
            return
        for source in queryset:
            old = {
                "approval_state": source.approval_state,
                "enabled": source.enabled,
            }
            if action == "approve":
                source.approval_state = JobSourceCatalog.ApprovalState.APPROVED
            elif action == "block":
                source.approval_state = JobSourceCatalog.ApprovalState.BLOCKED
            elif action == "enable":
                source.enabled = True
            else:
                source.enabled = False
            source.save(update_fields=["approval_state", "enabled", "modified"])
            new = {"approval_state": source.approval_state, "enabled": source.enabled}
            OperationalChangeAudit.record(
                actor=request.user,
                target_type="job_source",
                target_id=source.pk,
                action=action,
                old_value=old,
                new_value=new,
                confirmed=True,
            )
            monitoring.record_event(
                "operational_change",
                {
                    "action": action,
                    "capability": "job_source",
                    "confirmed": True,
                },
            )
        self.message_user(request, f"{queryset.count()} job source(s) updated and audited.")

    @admin.action(description="Enable selected job sources (confirm=yes)")
    def enable_sources(self, request, queryset):
        self._state_action(request, queryset, "enable")

    @admin.action(description="Disable selected job sources (confirm=yes)")
    def disable_sources(self, request, queryset):
        self._state_action(request, queryset, "disable")

    @admin.action(description="Approve selected job sources (confirm=yes)")
    def approve_sources(self, request, queryset):
        self._state_action(request, queryset, "approve")

    @admin.action(description="Block selected job sources (confirm=yes)")
    def block_sources(self, request, queryset):
        self._state_action(request, queryset, "block")

    @admin.action(description="Trigger crawl for selected job sources (confirm=yes)")
    def trigger_crawls(self, request, queryset):
        if getattr(request, "POST", {}).get("confirm") != "yes":
            self.message_user(request, "No crawls started. Repeat the action with confirm=yes.", level="warning")
            return
        started = 0
        for source in queryset:
            try:
                trigger_crawl(source_key=source.adapter_key, source_type="job", requested_by=request.user)
                started += 1
            except CrawlRequestError as exc:
                self.message_user(request, f"{source.name}: {exc}", level="error")
        self.message_user(request, f"{started} crawl(s) triggered and audited.")


class JobListingAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    """Staff-only listing diagnosis; avoid exposing description excerpts in lists."""

    model = JobListing
    list_display = ["title", "employer_name", "source", "status", "is_remote", "last_seen_at"]
    list_filter = ["status", "is_remote", "source"]
    list_select_related = ["source"]
    search_fields = ["title", "employer_name", "employer_domain", "external_id"]
    readonly_fields = [
        "source", "external_id", "canonical_url", "employer_name", "employer_domain",
        "title", "location_text", "is_remote", "compensation_min", "compensation_max",
        "compensation_currency", "compensation_interval", "description_excerpt",
        "first_seen_at", "last_seen_at", "status", "source_metadata", "created", "modified",
    ]


admin.site.register(JobSourceCatalog, JobSourceCatalogAdmin)
admin.site.register(JobListing, JobListingAdmin)


class CrawlRunAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = CrawlRun
    list_display = ["source_type", "source_key", "outcome", "requested_by", "started_at", "finished_at"]
    list_filter = ["source_type", "outcome"]
    search_fields = ["source_key", "error_summary"]
    readonly_fields = ["source_type", "source_key", "source", "job_source", "requested_by", "agent_run", "started_at", "finished_at", "outcome", "counts", "error_summary", "created", "modified"]


admin.site.register(CrawlRun, CrawlRunAdmin)


class CapabilitySwitchAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    """Staff-only kill switches; every toggle requires explicit confirmation."""

    model = CapabilitySwitch
    list_display = ["key", "enabled", "note", "modified"]
    list_filter = ["enabled"]
    search_fields = ["key", "note"]
    readonly_fields = ["created", "modified"]
    actions = ["enable_capabilities", "disable_capabilities"]

    def _toggle(self, request, queryset, enabled):
        post = getattr(request, "POST", None)
        if post is not None and post.get("confirm") != "yes":
            self.message_user(
                request,
                "No changes made. Repeat the action with confirm=yes.",
                level="warning",
            )
            return
        action = "enable" if enabled else "disable"
        for switch in queryset:
            old = {"enabled": switch.enabled}
            switch.enabled = enabled
            switch.save(update_fields=["enabled", "modified"])
            OperationalChangeAudit.record(
                actor=request.user,
                target_type="capability",
                target_id=switch.key,
                action=action,
                old_value=old,
                new_value={"enabled": enabled},
                confirmed=True,
            )
            monitoring.record_event(
                "operational_change",
                {
                    "action": action,
                    "capability": switch.key,
                    "confirmed": True,
                },
            )
        self.message_user(request, f"{queryset.count()} capability switch(es) updated and audited.")

    @admin.action(description="Enable selected capabilities (confirm=yes)")
    def enable_capabilities(self, request, queryset):
        self._toggle(request, queryset, True)

    @admin.action(description="Disable selected capabilities (confirm=yes)")
    def disable_capabilities(self, request, queryset):
        self._toggle(request, queryset, False)


class OperationalChangeAuditAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = OperationalChangeAudit
    list_display = ["actor", "action", "target_type", "target_id", "confirmed", "created"]
    list_filter = ["action", "target_type", "confirmed"]
    search_fields = ["target_id", "actor__username"]
    readonly_fields = [
        "actor", "target_type", "target_id", "action", "old_value", "new_value",
        "confirmed", "created", "modified",
    ]


admin.site.register(CapabilitySwitch, CapabilitySwitchAdmin)
admin.site.register(OperationalChangeAudit, OperationalChangeAuditAdmin)


class JobMatchAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = JobMatch
    list_display = [
        "user", "listing", "organization", "preference_version", "ranker_version",
        "score", "seen_at", "dismissed", "last_matched_at",
    ]
    list_filter = ["dismissed", "ranker_version", "preference_version"]
    list_select_related = ["user", "listing", "organization"]
    search_fields = ["user__username", "user__email", "listing__title", "listing__employer_name"]
    readonly_fields = [
        "user", "listing", "organization", "preference_version", "ranker_version", "score",
        "factors", "first_matched_at", "last_matched_at", "seen_at", "dismissed",
        "created", "modified",
    ]


admin.site.register(JobMatch, JobMatchAdmin)


class EmployerAliasAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = EmployerAlias
    list_display = ["kind", "value", "organization", "status", "reviewed_by", "reviewed_at"]
    list_filter = ["kind", "status"]
    list_select_related = ["organization", "reviewed_by"]
    search_fields = ["value", "organization__name"]
    readonly_fields = ["created", "modified"]
    actions = ["approve_aliases", "reject_aliases"]

    def _set_status(self, request, queryset, status):
        from django.utils import timezone as dj_tz
        from crank.agents.jobs.employer import reprocess_employer_alias

        count = 0
        for alias in queryset:
            alias.status = status
            alias.reviewed_by = request.user
            alias.reviewed_at = dj_tz.now()
            alias.save(update_fields=["status", "reviewed_by", "reviewed_at", "modified"])
            if status == EmployerAlias.Status.APPROVED:
                reprocess_employer_alias(alias)
            count += 1
        self.message_user(request, f"{count} employer alias(es) updated.")

    @admin.action(description="Approve selected employer aliases")
    def approve_aliases(self, request, queryset):
        self._set_status(request, queryset, EmployerAlias.Status.APPROVED)

    @admin.action(description="Reject selected employer aliases")
    def reject_aliases(self, request, queryset):
        self._set_status(request, queryset, EmployerAlias.Status.REJECTED)


class UnresolvedEmployerAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    model = UnresolvedEmployer
    list_display = ["listing", "employer_name", "employer_domain", "reason", "resolved", "resolved_at"]
    list_filter = ["reason", "resolved"]
    list_select_related = ["listing"]
    search_fields = ["employer_name", "employer_domain"]
    readonly_fields = [
        "listing", "employer_name", "employer_domain", "reason", "candidates",
        "resolved", "resolved_at", "created", "modified",
    ]


admin.site.register(EmployerAlias, EmployerAliasAdmin)
admin.site.register(UnresolvedEmployer, UnresolvedEmployerAdmin)


# ---------------------------------------------------------------------------
# Job Retrieval Operations dashboard (issue #404)
# ---------------------------------------------------------------------------
class JobRetrievalOps(models.Model):
    """Proxy model for the Job Retrieval Operations admin dashboard.

    This model exists solely to register a custom admin view that aggregates
    job-source readiness, counts, and bounded audited queue actions. It has no
    database table and never stores data.
    """

    class Meta:
        app_label = "crank"
        managed = False
        verbose_name = "Job Retrieval Operations"
        verbose_name_plural = "Job Retrieval Operations"


from crank.admin_dashboard import JobRetrievalOperationsAdmin

admin.site.register(JobRetrievalOps, JobRetrievalOperationsAdmin)
