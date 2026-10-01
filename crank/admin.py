# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import hashlib
import json

from django.contrib import admin
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.core import checks
from django import forms
from django.db import models, transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from crank.models.agent_run import AgentRun
from crank.models.crawl_run import CrawlRun
from crank.models.conversation import Conversation, Message
from crank.models.employer import EmployerAlias, UnresolvedEmployer
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.organization import Organization
from crank.models.company_profile import CompanyFieldEvidence, CompanyProfileObservation
from crank.models.company_request import CompanyRequest
from crank.models.preference import UserPreference, UserPreferenceAudit
from crank.models.score import Score, ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight
from crank.models.source import ApprovalState, SourceCatalog, SourceRun, SourceCatalogAudit
from crank.models.monitoring import CapabilitySwitch, OperationalChangeAudit
from crank.services import company_evidence, monitoring
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


class ConfirmableAdminActionMixin:
    """Shared intermediate-confirmation path for gated changelist actions.

    Stock Django admin changelist action POSTs never send the ``confirm``
    field, so any action that strides its own mutating body behind that gate
    becomes a silent no-op for real staff. This mixin closes the loop by
    intercepting unconfirmed action POSTs in :meth:`response_action` and
    re-rendering them as a confirmation page (listing the selected objects and
    the action) instead of mutating state. The confirmation form re-POSTs the
    same action with a hidden ``confirm=yes`` input (plus csrf/
    ``_selected_action``/``action``/``index``), so the gate and the UI can
    never drift. Unconfirmed POSTs stay no-ops; only the confirmed path runs
    the action body that records ``OperationalChangeAudit(confirmed=True)``.

    Action bodies must still guard themselves with
    :meth:`_require_confirmation` so a direct/in-band call without
    ``confirm=yes`` cannot mutate state either.

    **Transaction policy (documented MINOR-1 choice).** Every confirmable action
    wraps its mutations and audit writes in a single ``transaction.atomic()``,
    giving whole-action (all-or-nothing) atomicity: a multi-row batch either
    fully applies *with* its audit rows or fully rolls back. No partial batch is
    ever persisted, so a failed batch can be retried without double-applying the
    rows that succeeded before the failure point. This is a deliberate trade
    against per-row atomicity (which would preserve earlier good rows but could
    leave a visibly split batch / interleaved audits).
    """

    _confirm_warning = (
        "No changes made. This action requires an explicit confirmation step; "
        "please review the selected objects and confirm below."
    )
    # Django's built-in delete confirmation handles ``delete_selected`` with its
    # own ``post=yes`` page; intercepting it here would trap the operator in an
    # infinite two-page confirmation loop, so it is excluded from the mixin.
    _delete_action_name = "delete_selected"
    # Like Django's own ``delete_confirmation_max_display``, bound how many
    # objects the ``select_across=1`` confirmation preview evaluates in memory.
    _confirm_max_display = 100
    # Every gated admin must enumerate the actions it guards here. ``check()``
    # asserts that no custom action drifts outside this allowlist, so a future
    # site-wide/custom action can't silently bypass the confirmation gate.
    confirmable_actions = ()
    # Copy of the confirmation page copy used to warn when the ``select_across``
    # matching set changes between preview and confirm.
    _drift_warning = (
        "The set of objects matching the current filters changed since you "
        "reviewed it. The action now applies to a different set than shown "
        "above; please review the current matching set and confirm again."
    )

    def check(self, **kwargs):
        """Fail loudly if a custom action isn't declared ``confirmable_actions``.

        The gate is enforced by convention (each action body must call
        ``_require_confirmation``); this system check makes drift detectable at
        startup/test time instead of only by review.
        """
        errors = super().check(**kwargs) if hasattr(super(), "check") else []
        declared = set(self.confirmable_actions or ())
        for name in self.actions or ():
            if name == self._delete_action_name or name in declared:
                continue
            errors.append(
                checks.Error(
                    f"Admin action {name!r} on {self.__class__.__name__} mutates "
                    f"state but is missing from confirmable_actions. Add it there "
                    f"and make sure the action body calls _require_confirmation().",
                    id="crank.E422",
                )
            )
        return errors

    def _confirmed(self, request):
        """True only when the POST supplies ``confirm=yes``.

        Real admin action POSTs always carry a POST QueryDict (even plain GETs
        get an empty one), so an absent ``request.POST`` is *not* treated as
        confirmed - closing the bypass where a non-standard request without a
        POST attribute would silently confirm a gated action.
        """
        post = getattr(request, "POST", None)
        if post is None:
            return False
        return post.get("confirm") == "yes"

    def _require_confirmation(self, request):
        """Warn and return False unless the POST is explicitly confirmed."""
        if self._confirmed(request):
            return True
        self.message_user(request, self._confirm_warning, level="warning")
        return False

    def response_action(self, request, queryset, **kwargs):
        """Intercept unconfirmed gated action POSTs with a confirmation page.

        Django's built-in ``delete_selected`` is deliberately excluded (see
        ``_delete_action_name``): it has its own confirmation page keyed on
        ``request.POST['post']``, and intercepting it would bounce the operator
        forever between the two confirmation pages without ever deleting.

        Malformed/no-selection POSTs fall through to Django's native action-form
        validation (native "no action selected" / "items must be selected"
        warnings) instead of rendering a spurious confirmation page. Confirmed
        ``select_across=1`` POSTs are re-checked against the previewed count and
        re-confirmed on drift so the operator never acts on a result set they
        didn't review.
        """
        action_name = self._requested_action(request)
        if action_name == self._delete_action_name:
            return super().response_action(request, queryset, **kwargs)
        if not action_name or action_name not in self.get_actions(request):
            # Unknown/blank action: don't show the gate's confirmation page.
            self.message_user(request, _("No action selected."), level="warning")
            return None
        if not queryset and not self._select_across_flag(request):
            # Nothing to confirm: delegate so Django issues its native
            # "items must be selected" warning rather than a spurious page.
            return super().response_action(request, queryset, **kwargs)
        if not self._require_confirmation(request):
            return self.render_action_confirmation(request, queryset)
        if self._count_drifted(request, queryset):
            # MINOR: the matching set changed since the operator reviewed it.
            return self.render_action_confirmation(request, queryset, drift=True)
        return super().response_action(request, queryset, **kwargs)

    def _select_across_flag(self, request):
        """True when the changelist form requested select-across."""
        return request.POST.get("select_across", "0") == "1"

    def _count_drifted(self, request, queryset):
        """Whether the reviewed ``select_across`` set changed before confirm.

        Only applies when the operator confirmed a ``select_across=1`` preview
        that carried a ``confirmed_total`` snapshot; absent that value (e.g. a
        direct/older flow) no drift gate is imposed. A malformed (non-integer)
        snapshot is treated as drift so a tampered value can never silently
        bypass the gate and act on an unreviewed set.
        """
        if not self._select_across_flag(request):
            return False
        reviewed = request.POST.get("confirmed_total")
        if reviewed is None:
            return False
        try:
            reviewed = int(reviewed)
        except (TypeError, ValueError):
            return True
        return reviewed >= 0 and queryset.count() != reviewed

    def _requested_action(self, request):
        """Return the action name the changelist form requested."""
        try:
            index = int(request.POST.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        try:
            return request.POST.getlist("action")[index]
        except IndexError:
            # Malformed/missing action list: fall back to the single field.
            return request.POST.get("action", "")

    def _action_description(self, request, action_name):
        """Resolve the changelist dropdown label for ``action_name``."""
        for name, description in self.get_action_choices(request):
            if name == action_name:
                return description
        return action_name

    def confirmation_label(self, obj):
        """Text shown for ``obj`` on the confirmation page; empty means ``str(obj)``."""
        return ""

    def confirmation_label_for_action(self, obj, action_name):
        """Like :meth:`confirmation_label`, for admins whose text depends on the action."""
        return self.confirmation_label(obj)

    def confirmation_hidden_fields(self, request, objects):
        """Extra ``(name, value)`` pairs the confirmation form re-POSTs."""
        return []

    def render_action_confirmation(self, request, queryset, drift=False):
        """Render a confirmation page that re-POSTs the same gated action.

        For ``select_across=1`` the queryset is truncated to
        ``_confirm_max_display`` items (mirroring Django's own delete
        confirmation) so large filtered sets never load the whole table into
        memory or emit a giant page; the form still re-POSTs ``select_across=1``
        with a ``confirmed_total`` snapshot so the confirmed POST can detect
        when the matching set drifted from what the operator reviewed.
        """
        action_name = self._requested_action(request)
        selected_pks = request.POST.getlist(ACTION_CHECKBOX_NAME)
        select_across = self._select_across_flag(request)
        if select_across:
            total = queryset.count()
            objects = list(queryset[: self._confirm_max_display])
            truncated = total > self._confirm_max_display
        else:
            objects = list(queryset.filter(pk__in=selected_pks))
            total = len(objects)
            truncated = False
        for obj in objects:
            obj.confirmation_label = self.confirmation_label_for_action(obj, action_name)
        return render(
            request,
            "admin/confirm_action.html",
            {
                **self.admin_site.each_context(request),
                "title": "Confirm admin action",
                "action_name": action_name,
                "action_description": self._action_description(
                    request, action_name
                ),
                "objects": objects,
                "total_count": total,
                "truncated": truncated,
                "drift": drift,
                "drift_warning": self._drift_warning,
                "selected_pks": selected_pks,
                "extra_hidden": self.confirmation_hidden_fields(request, objects),
                "select_across": "1" if select_across else "0",
                "action_checkbox_name": ACTION_CHECKBOX_NAME,
                "object_label": self.opts.verbose_name,
            },
        )


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


class _ScopedRefusal(Exception):
    pass


class CompanyProfileObservationAdmin(ConfirmableAdminActionMixin, StaffOnlyAdminMixin, admin.ModelAdmin):
    model = CompanyProfileObservation
    list_display = [
        "observed_name", "observed_domain", "organization", "status",
        "observed_at", "extraction_version",
    ]
    list_filter = ["status", "extraction_version"]
    list_select_related = ["organization", "reviewed_by"]
    search_fields = ["observed_name", "observed_domain", "source_url"]
    readonly_fields = [
        "status", "organization", "source_url", "observed_domain", "observed_name",
        "description", "locations", "rto_evidence", "funding_evidence",
        "public_status_evidence", "logo_url", "brand_metadata", "observed_at",
        "extraction_version", "conflict_fields", "fingerprint", "created",
        "modified", "reviewed_by", "reviewed_at",
    ]
    actions = ["accept_observations", "reject_observations", "conflict_observations"]
    confirmable_actions = ["accept_observations", "reject_observations", "conflict_observations"]

    def confirmation_label_for_action(self, obj, action_name):
        if action_name == "reject_observations":
            return self._reject_label(obj)
        if action_name == "accept_observations":
            return self.confirmation_label(obj)
        return self._base_label(obj)

    @staticmethod
    def _base_label(obj):
        return f"{obj.observed_name or obj.observed_domain} [{obj.status}] {obj.source_url}"

    def _reject_label(self, obj):
        label = self._base_label(obj)
        if obj.status not in self._RETRACTING_STATUSES:
            return label
        rows = company_evidence.retractable_observation_facts(obj)
        if not rows:
            return label + "; rejecting withdraws no verified facts"
        facts = "; ".join(f"{row.field_key}={row.value_text!r}" for row in rows)
        return (
            label + "; rejecting WITHDRAWS these verified facts from matching "
            f"(they become superseded): {facts}"
        )

    _RETRACTING_STATUSES = (
        CompanyProfileObservation.Status.ACCEPTED,
        CompanyProfileObservation.Status.AUTO_APPLIED,
    )

    def confirmation_label(self, obj):
        label = self._base_label(obj)
        if obj.organization_id is None:
            return label
        values = company_evidence.observation_field_values(obj)
        review = {
            key: value for key, value in values.items()
            if key in company_evidence.REVIEW_REQUIRED_FIELDS
        }
        accepted = {
            row.field_key: row
            for row in CompanyFieldEvidence.objects.filter(
                organization_id=obj.organization_id,
                state=CompanyFieldEvidence.State.ACCEPTED,
                field_key__in=list(values),
            ).order_by("-observed_at", "-id")
        }
        parts = []
        for key, value in sorted(values.items()):
            row = accepted.get(key)
            note = ""
            if row is not None and company_evidence.list_scope(row.scope_json):
                note = f" (keeps scope {company_evidence.list_scope(row.scope_json)})"
            marker = " [verifies policy fact]" if key in review else ""
            if row is not None and row.value_text != value:
                kind = (
                    "staff-reviewed" if company_evidence.is_staff_reviewed(row)
                    else "not staff-reviewed"
                )
                marker += f" [REPLACES {row.value_text!r} ({kind})]"
            reading = company_evidence.matching_reading(key, value)
            if reading:
                marker += f" [{reading}]"
            parts.append(f"{key}={value!r}{marker}{note}")
        if obj.status != CompanyProfileObservation.Status.ACCEPTED and parts:
            label += "; accepting records as accepted evidence: " + "; ".join(parts)
        return label

    def observations_digest(self, observations):
        """Digest of what a reviewer saw: each observation, its carried values and
        the accepted row each value would replace."""
        entries = []
        for obs in sorted(observations, key=lambda o: o.pk):
            values = company_evidence.observation_field_values(obs)
            accepted = {}
            if obs.organization_id is not None:
                accepted = {
                    row.field_key: row
                    for row in CompanyFieldEvidence.objects.filter(
                        organization_id=obs.organization_id,
                        state=CompanyFieldEvidence.State.ACCEPTED,
                        field_key__in=list(values),
                    ).order_by("-observed_at", "-id")
                }
            entries.append(
                [
                    obs.pk,
                    obs.status,
                    {k: company_evidence.value_digest(v) for k, v in sorted(values.items())},
                    {
                        k: [row.pk, company_evidence.value_digest(row.value_text), row.scope_json]
                        for k, row in sorted(accepted.items())
                    },
                ]
            )
        return hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def confirmation_hidden_fields(self, request, objects):
        return [("observation_digest", self.observations_digest(objects))]

    def response_action(self, request, queryset, **kwargs):
        if (
            self._select_across_flag(request)
            and self._requested_action(request)
            in ("accept_observations", "reject_observations")
        ):
            self.message_user(
                request,
                "No changes made: observations must be selected explicitly. A select-across "
                "accept could verify, and a select-across reject could withdraw, policy facts "
                "for observations you never saw; select the rows on the page instead.",
                level="error",
            )
            return None
        return super().response_action(request, queryset, **kwargs)

    def _review(self, request, queryset, status):
        if not self._require_confirmation(request):
            return
        count = 0
        refused = []
        rejected_values = []
        accepting = status == CompanyProfileObservation.Status.ACCEPTED
        with transaction.atomic():
            if accepting:
                # Lock the organizations first, then re-read and compare the
                # digest in the same transaction, so a concurrent accept that
                # committed after the reviewer looked is never overwritten.
                observations = list(queryset)
                company_evidence.lock_organizations(o.organization_id for o in observations)
                observations = list(
                    CompanyProfileObservation.objects.filter(
                        pk__in=[o.pk for o in observations]
                    ).order_by("pk")
                )
                if request.POST.get("observation_digest") != self.observations_digest(
                    observations
                ):
                    self.message_user(
                        request,
                        "No changes made: the selected observations (or the values they would "
                        "replace) changed since you reviewed them. Review the current ones and "
                        "confirm again.",
                        level="error",
                    )
                    return
                queryset = observations
            for observation in queryset:
                if accepting and company_evidence.scoped_accepted_conflicts(observation):
                    # Accepting whole would replace staff-scoped evidence with
                    # a company-wide row; scoped facts change only through
                    # their claims.
                    refused.append(observation.pk)
                    continue
                if accepting and company_evidence.rejected_value_conflicts(observation):
                    rejected_values.append(observation.pk)
                    continue
                try:
                    with transaction.atomic():
                        old_status = observation.status
                        observation.mark_reviewed(status=status, user=request.user)
                        created, superseded = [], []
                        if accepting:
                            # An operator accept is an ACCEPTED-producing surface, so
                            # it must create the same field-level evidence the
                            # crawler does (issue #460, AC-4) — and, through the same
                            # outbox event, the same provenance cache invalidation.
                            # Same transaction as the review state: evidence is never
                            # recorded for a review that did not commit.
                            before = self._accepted_ids(observation)
                            if observation.organization_id is not None:
                                try:
                                    with transaction.atomic():
                                        created = [
                                            row.pk
                                            for row in company_evidence.accept_observation_fields(
                                                observation
                                            )
                                        ]
                                except company_evidence.EvidenceNotAcceptable:
                                    # Scoped evidence appeared after the pre-check and
                                    # the locked check refused: nothing was written.
                                    raise _ScopedRefusal from None
                            superseded = sorted(before - self._accepted_ids(observation))
                        claim_ids = []
                        retracted = []
                        restated_closed, restated_conflicted = [], []
                        if accepting:
                            claim_ids = company_evidence.resolve_observation_claims(
                                observation, company_evidence.State.SUPERSEDED
                            )
                            now = timezone.now()
                            for row in CompanyFieldEvidence.objects.filter(pk__in=created):
                                closed, conflicted = company_evidence.restate_open_claims(
                                    row.organization_id, row.field_key, row.value_text, now
                                )
                                restated_closed += closed
                                restated_conflicted += conflicted
                        elif status == CompanyProfileObservation.Status.REJECTED:
                            # ``superseded``, not ``rejected``: a claim is a value
                            # staff decided on, and rejecting a whole observation says
                            # nothing about each value it carried.
                            claim_ids = company_evidence.resolve_observation_claims(
                                observation, company_evidence.State.SUPERSEDED
                            )
                            if old_status in self._RETRACTING_STATUSES:
                                retracted = company_evidence.retract_observation_facts(
                                    observation
                                )
                        OperationalChangeAudit.record(
                            actor=request.user,
                            target_type="company_profile_observation",
                            target_id=observation.pk,
                            action=f"review_{status}",
                            old_value={"status": old_status},
                            new_value={
                                "status": status,
                                "evidence_created": created,
                                "evidence_superseded": superseded,
                                "claims_resolved": claim_ids,
                                "claims_closed": restated_closed,
                                "claims_conflicted": restated_conflicted,
                                "evidence_retracted": retracted,
                            },
                            confirmed=True,
                        )
                        for evidence_id in created:
                            OperationalChangeAudit.record(
                                actor=request.user,
                                target_type="company_field_evidence",
                                target_id=evidence_id,
                                action="observation_accepted",
                                old_value={},
                                new_value={"observation": observation.pk},
                                confirmed=True,
                            )
                except _ScopedRefusal:
                    observation.refresh_from_db()
                    refused.append(observation.pk)
                    continue
                count += 1
        self.message_user(request, f"{count} company profile observation(s) marked {status}.")
        if refused:
            self.message_user(
                request,
                f"{len(refused)} observation(s) not accepted: accepting them would replace "
                "scoped evidence. Review their claims instead.",
                level="error",
            )
        if rejected_values:
            self.message_user(
                request,
                f"{len(rejected_values)} observation(s) not accepted: they carry a value staff "
                "recently rejected. Review their claims instead.",
                level="error",
            )

    @staticmethod
    def _accepted_ids(observation):
        if observation.organization_id is None:
            return set()
        return set(
            CompanyFieldEvidence.objects.filter(
                organization_id=observation.organization_id,
                state=CompanyFieldEvidence.State.ACCEPTED,
            ).values_list("pk", flat=True)
        )

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


class CompanyFieldEvidenceScopeForm(forms.ModelForm):
    """Validate the staff-editable scope of a claim.

    The rules live in ``company_evidence.validate_claim_scope`` (also enforced
    by ``accept_claim``). ``claimed_domain`` is crawler provenance: it is
    carried over from the stored row, never editable here.
    """

    class Meta:
        model = CompanyFieldEvidence
        fields = ["scope_json"]
        help_texts = {
            "scope_json": (
                "Optional JSON object with countries and/or role_families (lists of at most "
                "10 short strings). Matching is case-insensitive and whole-word: each country "
                "must appear as a word in the listing's location text (use names such as "
                "\"United States\", not codes such as \"US\", which never match) and each "
                "role family as a word in the job title. Facts whose scope does not match a "
                "listing stop applying to it. Team scope is not supported."
            )
        }

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and self.instance.state not in company_evidence.OPEN_CLAIM_STATES:
            raise forms.ValidationError(
                f"Scope not saved: claim state {self.instance.state!r} is not open for review."
            )
        return cleaned

    def clean_scope_json(self):
        scope = self.cleaned_data.get("scope_json")
        if isinstance(scope, dict):
            scope = {key: value for key, value in scope.items() if key != "claimed_domain"}
        try:
            cleaned = company_evidence.validate_claim_scope(scope)
        except company_evidence.EvidenceNotAcceptable as exc:
            raise forms.ValidationError(str(exc))
        stored = (self.instance.scope_json or {}).get("claimed_domain")
        if stored:
            cleaned["claimed_domain"] = stored
        return cleaned


class OpenClaimFilter(admin.SimpleListFilter):
    """Default the changelist to claims awaiting review.

    An explicit ``state`` filter takes over, so picking Accepted, Superseded
    or Rejected is never AND-ed with the open-claims default.
    """

    title = "review queue"
    parameter_name = "queue"

    def __init__(self, request, params, model, model_admin):
        self._explicit_state = "state__exact" in request.GET or "legacy" in request.GET
        super().__init__(request, params, model, model_admin)

    def lookups(self, request, model_admin):
        return [("open", "Open claims (default)"), ("all", "All evidence")]

    def queryset(self, request, queryset):
        if self.value() == "all" or self._explicit_state:
            return queryset
        return queryset.filter(state__in=company_evidence.OPEN_CLAIM_STATES)

    def choices(self, changelist):
        for lookup, title in self.lookup_choices:
            yield {
                "selected": not self._explicit_state and (self.value() or "open") == lookup,
                "query_string": changelist.get_query_string({self.parameter_name: lookup}),
                "display": title,
            }


class LegacyUnreviewedFilter(admin.SimpleListFilter):
    """Accepted review-required rows no person reviewed (pre-#474 crawl acceptances)."""

    title = "legacy rows"
    parameter_name = "legacy"

    def lookups(self, request, model_admin):
        return [("unreviewed", "Accepted, never staff-reviewed")]

    def queryset(self, request, queryset):
        if self.value() != "unreviewed":
            return queryset
        ids = [row.pk for row in company_evidence.legacy_unreviewed_rows()]
        return queryset.filter(pk__in=ids)


class CompanyFieldEvidenceAdmin(ConfirmableAdminActionMixin, StaffOnlyAdminMixin, admin.ModelAdmin):
    model = CompanyFieldEvidence
    form = CompanyFieldEvidenceScopeForm
    list_display = ["organization", "field_key", "value_text", "matching_reading", "state", "relation_to_accepted", "source_url", "observed_at", "last_verified_at"]
    list_filter = [OpenClaimFilter, LegacyUnreviewedFilter, "state", "field_key"]
    list_select_related = ["organization"]
    search_fields = ["organization__name", "value_text", "source_domain"]
    actions = ["accept_claims", "reject_claims"]
    confirmable_actions = ["accept_claims", "reject_claims"]

    _model_fields = [
        "organization", "field_key", "value_text", "source_url", "source_domain",
        "observation", "scope_json", "observed_at", "validation_version",
        "extractor_version", "state", "last_checked_at", "last_successful_fetch_at",
        "last_changed_at", "last_verified_at", "created", "modified",
    ]

    def get_fields(self, request, obj=None):
        return self._model_fields

    def get_readonly_fields(self, request, obj=None):
        readonly = [f for f in self._model_fields if f != "scope_json"]
        if obj is not None and obj.state not in company_evidence.OPEN_CLAIM_STATES:
            readonly.append("scope_json")
        return readonly

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _accepted_row(self, obj):
        return (
            CompanyFieldEvidence.objects.filter(
                organization_id=obj.organization_id,
                field_key=obj.field_key,
                state=CompanyFieldEvidence.State.ACCEPTED,
            )
            .order_by("-observed_at", "-id")
            .first()
        )

    def confirmation_label(self, obj):
        accepted = self._accepted_row(obj)
        current_scope = company_evidence.list_scope(accepted.scope_json) if accepted else {}
        new_scope = company_evidence.list_scope(
            company_evidence.accepted_scope_for_claim(obj, accepted)
        )
        scope_text = f"scope {new_scope or 'company-wide'}"
        if accepted is not None and current_scope != new_scope:
            scope_text += f" (CHANGES the accepted scope {current_scope or 'company-wide'})"
        legacy = ""
        if (
            accepted is not None
            and accepted.value_text == obj.value_text
            and not company_evidence.is_staff_reviewed(accepted)
        ):
            legacy = " [re-confirms a legacy value already in effect; rejecting retracts it]"
        reading = company_evidence.matching_reading(obj.field_key, obj.value_text)
        reading = f"; {reading}" if reading else ""
        return (
            f"{obj.organization.name} / {obj.field_key} [{obj.state}]: "
            f"proposed {obj.value_text!r}{reading}; currently accepted "
            f"{repr(accepted.value_text) if accepted is not None else 'none'}{legacy}; "
            f"{scope_text}; source {obj.source_url}"
        )

    def get_changelist_instance(self, request):
        """Resolve the "vs accepted" column for the whole page in two queries."""
        cl = super().get_changelist_instance(request)
        rows = [row for row in cl.result_list if row.state in company_evidence.OPEN_CLAIM_STATES]
        if rows:
            accepted = {}
            for row in CompanyFieldEvidence.objects.filter(
                organization_id__in={r.organization_id for r in rows},
                field_key__in={r.field_key for r in rows},
                state=CompanyFieldEvidence.State.ACCEPTED,
            ).select_related("observation").order_by("observed_at", "id"):
                accepted[(row.organization_id, row.field_key)] = row
            reviewed = company_evidence.staff_reviewed_ids(accepted.values())
            for row in rows:
                current = accepted.get((row.organization_id, row.field_key))
                if current is None:
                    row._vs_accepted = "no accepted value"
                elif current.value_text != row.value_text:
                    row._vs_accepted = "differs from accepted"
                elif current.pk in reviewed:
                    row._vs_accepted = "matches reviewed value"
                else:
                    row._vs_accepted = "legacy value in effect"
        return cl

    @admin.display(description="matching reads")
    def matching_reading(self, obj):
        return company_evidence.matching_reading(obj.field_key, obj.value_text)

    @admin.display(description="vs accepted")
    def relation_to_accepted(self, obj):
        if obj.state not in company_evidence.OPEN_CLAIM_STATES:
            return ""
        if hasattr(obj, "_vs_accepted"):
            return obj._vs_accepted
        accepted = self._accepted_row(obj)
        if accepted is None:
            return "no accepted value"
        if accepted.value_text != obj.value_text:
            return "differs from accepted"
        if company_evidence.is_staff_reviewed(accepted):
            return "matches reviewed value"
        return "legacy value in effect"

    def claims_digest(self, claims):
        """Digest of exactly what a reviewer saw: each claim, its value hash, state,
        scope and the accepted row it would replace."""
        entries = []
        for claim in sorted(claims, key=lambda c: c.pk):
            accepted = self._accepted_row(claim)
            entries.append(
                [
                    claim.pk,
                    company_evidence.value_digest(claim.value_text),
                    claim.state,
                    claim.scope_json,
                    [
                        accepted.pk,
                        company_evidence.value_digest(accepted.value_text),
                        accepted.scope_json,
                    ] if accepted else None,
                ]
            )
        return hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def confirmation_hidden_fields(self, request, objects):
        return [("claim_digest", self.claims_digest(objects))]

    def response_action(self, request, queryset, **kwargs):
        if (
            self._select_across_flag(request)
            and self._requested_action(request) in self.confirmable_actions
        ):
            self.message_user(
                request,
                "No changes made: claims must be selected explicitly. A select-across "
                "decision could act on claims you never saw; select the rows on the page "
                "instead.",
                level="error",
            )
            return None
        return super().response_action(request, queryset, **kwargs)

    def save_model(self, request, obj, form, change):
        try:
            company_evidence.update_claim_scope(obj, obj.scope_json, reviewer=request.user)
        except company_evidence.EvidenceNotAcceptable as exc:
            request._scope_save_refused = True
            self.message_user(request, f"Scope not saved: {exc}", level="error")

    def log_change(self, request, obj, message):
        if getattr(request, "_scope_save_refused", False):
            return None
        return super().log_change(request, obj, message)

    def response_change(self, request, obj):
        if getattr(request, "_scope_save_refused", False):
            return HttpResponseRedirect(request.path)
        return super().response_change(request, obj)

    def _decide(self, request, queryset, decision):
        if not self._require_confirmation(request):
            return
        applied = skipped = 0
        with transaction.atomic():
            claims = list(queryset)
            if not claims:
                self.message_user(
                    request,
                    "No changes made: none of the selected claims exist any more.",
                    level="error",
                )
                return
            # Lock the organizations first, then re-read and compare the digest in
            # this transaction: a decision committed after the reviewer looked
            # must make this one refuse, not be silently superseded.
            company_evidence.lock_organizations(c.organization_id for c in claims)
            claims = list(
                CompanyFieldEvidence.objects.filter(pk__in=[c.pk for c in claims])
                .select_related("organization")
                .order_by("pk")
            )
            if request.POST.get("claim_digest") != self.claims_digest(claims):
                self.message_user(
                    request,
                    "No changes made: the selected claims changed (or were replaced by a new "
                    "crawl) since you reviewed them. Review the current claims and confirm again.",
                    level="error",
                )
                return
            if decision is company_evidence.accept_claim:
                seen = {}
                for claim in claims:
                    if claim.state in company_evidence.OPEN_CLAIM_STATES:
                        seen.setdefault((claim.organization_id, claim.field_key), set()).add(
                            (claim.value_text, json.dumps(company_evidence.list_scope(claim.scope_json), sort_keys=True))
                        )
                if any(len(values) > 1 for values in seen.values()):
                    self.message_user(
                        request,
                        "No changes made: the selection has claims with different values for "
                        "the same organization and field (or the same value with different scopes). "
                        "Accept one claim per field; the "
                        "others become conflicted or superseded.",
                        level="error",
                    )
                    return
            for claim in claims:
                try:
                    decision(claim, reviewer=request.user)
                except company_evidence.EvidenceNotAcceptable:
                    skipped += 1
                    continue
                applied += 1
        return applied, skipped

    @admin.action(description="Accept selected open claims as verified evidence")
    def accept_claims(self, request, queryset):
        result = self._decide(request, queryset, company_evidence.accept_claim)
        if result:
            self.message_user(request, f"{result[0]} claim(s) accepted; {result[1]} skipped (not open, rejected observation or invalid scope).")

    @admin.action(description="Reject selected open claims")
    def reject_claims(self, request, queryset):
        result = self._decide(request, queryset, company_evidence.reject_claim)
        if result:
            self.message_user(request, f"{result[0]} claim(s) rejected; {result[1]} skipped (not open).")


admin.site.register(CompanyFieldEvidence, CompanyFieldEvidenceAdmin)
class CompanyRequestAdmin(ConfirmableAdminActionMixin, StaffOnlyAdminMixin, admin.ModelAdmin):
    model = CompanyRequest
    list_display = ["company_name", "website_url", "requester", "status", "created", "crawl_source_approved", "refresh_queued"]
    list_filter = ["status", "crawl_source_approved", "refresh_queued"]
    search_fields = ["company_name", "normalized_domain", "requester__username", "requester__email"]
    list_select_related = ["requester", "duplicate_of", "approved_organization"]
    readonly_fields = ["requester", "company_name", "normalized_name", "website_url", "normalized_domain", "careers_url", "reason", "status", "duplicate_of", "approved_organization", "created", "modified", "crawl_source_approved", "refresh_queued"]
    # Every action on this admin mutates state only after a shared
    # intermediate confirmation step (see ConfirmableAdminActionMixin).
    actions = ["approve_requests", "reject_requests", "mark_duplicate", "approve_crawl_sources", "queue_refresh"]
    # Allow-list enforced by ConfirmableAdminActionMixin.check().
    confirmable_actions = [
        "approve_requests", "reject_requests", "mark_duplicate",
        "approve_crawl_sources", "queue_refresh",
    ]

    def _audit(self, request, item, action, old, new):
        OperationalChangeAudit.record(actor=request.user, target_type="company_request", target_id=item.pk, action=action, old_value=old, new_value=new, confirmed=True)

    @admin.action(description="Approve and create pending organizations")
    def approve_requests(self, request, queryset):
        if not self._require_confirmation(request):
            return
        updated = 0
        # All-or-nothing whole-action transaction (documented MINOR-1 policy; see
        # ConfirmableAdminActionMixin): change + audit commit together or roll back.
        with transaction.atomic():
            for item in queryset.filter(status=CompanyRequest.Status.PENDING):
                organization = Organization.objects.create(name=item.company_name, url=item.website_url, status=0, public=True)
                old = {"status": item.status, "approved_organization": None}
                item.status = CompanyRequest.Status.APPROVED
                item.approved_organization = organization
                item.save(update_fields=["status", "approved_organization", "modified"])
                self._audit(request, item, "approve", old, {"status": item.status, "approved_organization": organization.pk})
                updated += 1
        self.message_user(request, f"{updated} suggestion(s) approved as pending organizations.")

    @admin.action(description="Reject selected suggestions")
    def reject_requests(self, request, queryset):
        if not self._require_confirmation(request):
            return
        updated = 0
        # All-or-nothing whole-action transaction (documented MINOR-1 policy).
        with transaction.atomic():
            for item in queryset.filter(status=CompanyRequest.Status.PENDING):
                old = {"status": item.status}
                item.status = CompanyRequest.Status.REJECTED
                item.save(update_fields=["status", "modified"])
                self._audit(request, item, "reject", old, {"status": item.status})
                updated += 1
        self.message_user(request, f"{updated} suggestion(s) rejected and audited.")

    @admin.action(description="Mark selected suggestions as duplicates")
    def mark_duplicate(self, request, queryset):
        if not self._require_confirmation(request):
            return
        updated = 0
        # All-or-nothing whole-action transaction (documented MINOR-1 policy).
        with transaction.atomic():
            for item in queryset.filter(status=CompanyRequest.Status.PENDING).exclude(duplicate_of=None):
                old = {"status": item.status, "duplicate_of": None}
                item.status = CompanyRequest.Status.DUPLICATE
                item.save(update_fields=["status", "modified"])
                self._audit(request, item, "duplicate", old, {"status": item.status, "duplicate_of": item.duplicate_of_id})
                updated += 1
        self.message_user(request, f"{updated} suggestion(s) marked as duplicates and audited.")

    @admin.action(description="Approve crawl source workflow")
    def approve_crawl_sources(self, request, queryset):
        if not self._require_confirmation(request):
            return
        updated = 0
        # All-or-nothing whole-action transaction (documented MINOR-1 policy).
        with transaction.atomic():
            for item in queryset.filter(status=CompanyRequest.Status.APPROVED, crawl_source_approved=False):
                item.crawl_source_approved = True
                item.save(update_fields=["crawl_source_approved", "modified"])
                self._audit(request, item, "approve_source", {"crawl_source_approved": False}, {"crawl_source_approved": True})
                updated += 1
        self.message_user(request, f"{updated} crawl source workflow(s) approved; no external calls were made.")

    @admin.action(description="Queue refresh after source approval")
    def queue_refresh(self, request, queryset):
        if not self._require_confirmation(request):
            return
        updated = 0
        # All-or-nothing whole-action transaction (documented MINOR-1 policy).
        with transaction.atomic():
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


class SourceCatalogAdmin(ConfirmableAdminActionMixin, StaffOnlyAdminMixin, admin.ModelAdmin):
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
        "last_crawl_at",
        "last_attempt_at",
        "consecutive_failures",
    ]
    list_filter = ["approval_state", "enabled", "cadence"]
    list_editable = ["enabled"]
    list_select_related = ["organization"]
    search_fields = ["name", "organization__name", "adapter_key"]
    readonly_fields = [
        "approved_at",
        "last_crawl_at",
        "last_attempt_at",
        "consecutive_failures",
        "created",
        "modified",
    ]
    filter_horizontal = ["supported_score_types"]
    actions = ["approve_sources", "block_sources", "enable_sources", "disable_sources", "trigger_crawls"]
    # Allow-list enforced by ConfirmableAdminActionMixin.check().
    confirmable_actions = [
        "approve_sources", "block_sources", "enable_sources",
        "disable_sources", "trigger_crawls",
    ]

    def save_model(self, request, obj, form, change):
        """Persist the row and record an audit (created/changed deltas).

        The write and its audit rows share one transaction so the promised
        ``OperationalChangeAudit``/``SourceCatalogAudit`` never detaches from
        the change it attests.
        """
        old = None
        if change:
            try:
                old = SourceCatalog.objects.get(pk=obj.pk)
            except SourceCatalog.DoesNotExist:
                old = None
        with transaction.atomic():
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
        if not self._require_confirmation(request):
            return
        updated = 0
        # All-or-nothing whole-action transaction (documented MINOR-1 policy).
        with transaction.atomic():
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

    @admin.action(description="Trigger crawl for selected sources")
    def trigger_crawls(self, request, queryset):
        if not self._require_confirmation(request):
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


class JobSourceCatalogAdmin(ConfirmableAdminActionMixin, StaffOnlyAdminMixin, admin.ModelAdmin):
    """Staff-only policy diagnosis without credentials or raw responses."""

    model = JobSourceCatalog
    inlines = [CrawlRunJobSourceInline]
    list_display = [
        "name", "adapter_key", "approval_state", "enabled", "base_url",
        "last_crawl_at", "last_attempt_at", "consecutive_failures",
        "created", "modified",
    ]
    list_filter = ["approval_state", "enabled"]
    search_fields = ["name", "adapter_key", "base_url"]
    readonly_fields = [
        "last_crawl_at", "last_attempt_at", "consecutive_failures",
        "created", "modified",
    ]
    actions = ["enable_sources", "disable_sources", "approve_sources", "block_sources", "trigger_crawls"]
    # Allow-list enforced by ConfirmableAdminActionMixin.check().
    confirmable_actions = [
        "enable_sources", "disable_sources", "approve_sources",
        "block_sources", "trigger_crawls",
    ]

    def _state_action(self, request, queryset, action):
        """Apply an operational state change only after explicit confirmation."""
        if not self._require_confirmation(request):
            return
        # All-or-nothing whole-action transaction (documented MINOR-1 policy).
        with transaction.atomic():
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

    @admin.action(description="Enable selected job sources")
    def enable_sources(self, request, queryset):
        self._state_action(request, queryset, "enable")

    @admin.action(description="Disable selected job sources")
    def disable_sources(self, request, queryset):
        self._state_action(request, queryset, "disable")

    @admin.action(description="Approve selected job sources")
    def approve_sources(self, request, queryset):
        self._state_action(request, queryset, "approve")

    @admin.action(description="Block selected job sources")
    def block_sources(self, request, queryset):
        self._state_action(request, queryset, "block")

    @admin.action(description="Trigger crawl for selected job sources")
    def trigger_crawls(self, request, queryset):
        if not self._require_confirmation(request):
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


class CapabilitySwitchAdmin(ConfirmableAdminActionMixin, StaffOnlyAdminMixin, admin.ModelAdmin):
    """Staff-only kill switches; every toggle requires explicit confirmation."""

    model = CapabilitySwitch
    list_display = ["key", "enabled", "note", "modified"]
    list_filter = ["enabled"]
    search_fields = ["key", "note"]
    readonly_fields = ["created", "modified"]
    actions = ["enable_capabilities", "disable_capabilities"]
    # Allow-list enforced by ConfirmableAdminActionMixin.check().
    confirmable_actions = ["enable_capabilities", "disable_capabilities"]

    def _toggle(self, request, queryset, enabled):
        if not self._require_confirmation(request):
            return
        action = "enable" if enabled else "disable"
        # All-or-nothing whole-action transaction (documented MINOR-1 policy).
        with transaction.atomic():
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

    @admin.action(description="Enable selected capabilities")
    def enable_capabilities(self, request, queryset):
        self._toggle(request, queryset, True)

    @admin.action(description="Disable selected capabilities")
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

    # Staff-reviewed evidence is derived from these rows, so staff can only read them.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
