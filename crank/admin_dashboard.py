# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Staff-only Job Retrieval Operations dashboard (issue #404).

A single discoverable end-to-end admin view aggregating job-source readiness,
counts, and bounded audited queue actions. All actions are confirm-gated and
record ``OperationalChangeAudit`` entries. No provider/network work is ever
performed inside the HTTP request.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import admin, messages
from django.db import IntegrityError, transaction
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone

from crank.admin import StaffOnlyAdminMixin
from crank.agents.jobs.base import APPROVED_JOB_SOURCE_DOMAINS
from crank.management.commands.seed_job_sources import SEED_SOURCES
from crank.models.agent_run import AgentRun
from crank.models.employer import UnresolvedEmployer
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.monitoring import OperationalChangeAudit
from crank.services import monitoring

logger = logging.getLogger(__name__)

# How stale (in hours) a listing must be before we consider it stale.
_STALE_HOURS = int(getattr(settings, "JOB_LISTING_STALE_HOURS", 24 * 7))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _is_allowed(host: str) -> bool:
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in APPROVED_JOB_SOURCE_DOMAINS
    )


def _aggregate_counts():
    """Compute dashboard aggregate counts in a single pass."""
    sources_qs = JobSourceCatalog.objects.all()
    configured = sources_qs.count()
    approved = sources_qs.filter(
        approval_state=JobSourceCatalog.ApprovalState.APPROVED
    ).count()
    enabled = sources_qs.filter(
        approval_state=JobSourceCatalog.ApprovalState.APPROVED, enabled=True
    ).count()

    # Active listings
    active_count = JobListing.objects.count()
    stale_threshold = timezone.now() - timezone.timedelta(hours=_STALE_HOURS)
    stale_count = JobListing.objects.filter(
        last_seen_at__lt=stale_threshold
    ).count()

    # Unresolved employers
    unresolved_count = UnresolvedEmployer.objects.filter(resolved=False).count()

    # Matches
    match_count = JobMatch.objects.count()

    # Latest pipeline run
    latest_run = (
        AgentRun.objects.filter(run_type=AgentRun.RunType.JOB_PIPELINE)
        .order_by("-created", "-id")
        .first()
    )

    latest_run_info = None
    if latest_run is not None:
        latest_run_info = {
            "status": latest_run.status,
            "created": latest_run.created.isoformat() if latest_run.created else None,
            "correlation_id": str(latest_run.correlation_id),
        }

    return {
        "configured": configured,
        "approved": approved,
        "enabled": enabled,
        "active_listings": active_count,
        "stale_listings": stale_count,
        "unresolved_employers": unresolved_count,
        "matches": match_count,
        "latest_run": latest_run_info,
    }


def _readiness_gates():
    """Compute safe readiness checks without touching the network."""
    from crank.agents.sources.registry import REGISTRY

    adapter_count = len(REGISTRY)

    # Credentials check: at least one job-source environment variable is set
    credentials_configured = bool(
        getattr(settings, "USAJOBS_AUTH_KEY", "").strip()
        or getattr(settings, "FIRECRAWL_API_KEY", "").strip()
    )

    pipeline_enabled = bool(getattr(settings, "JOB_PIPELINE_ENABLED", False))
    scheduler_enabled = bool(getattr(settings, "CRAWL_CRON_ENABLED", False))

    active_run = AgentRun.objects.filter(
        run_type=AgentRun.RunType.JOB_PIPELINE,
        status=AgentRun.Status.RUNNING,
    ).first()

    return {
        "adapter_registered": adapter_count > 0,
        "adapter_count": adapter_count,
        "credentials_configured": credentials_configured,
        "pipeline_enabled": pipeline_enabled,
        "scheduler_enabled": scheduler_enabled,
        "active_run": active_run is not None,
        "active_run_status": active_run.status if active_run else None,
    }


def _confirm(request):
    """Return True only when the request-body carries confirm=yes."""
    return request.POST.get("confirm") == "yes"


def _audit(request, action, old_value=None, new_value=None):
    OperationalChangeAudit.record(
        actor=request.user,
        target_type="job_retrieval_ops",
        target_id="dashboard",
        action=action,
        old_value=old_value or {},
        new_value=new_value or {},
        confirmed=True,
    )


class JobRetrievalOperationsAdmin(StaffOnlyAdminMixin, admin.ModelAdmin):
    """Staff-only dashboard for Job Retrieval Operations."""

    change_list_template = "admin/job_retrieval_operations.html"

    def has_add_permission(self, request):
        return self.has_view_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_view_permission(request)

    def get_urls(self):
        from django.urls import path

        info = self.model._meta.app_label, self.model._meta.model_name
        return [
            path(
                "",
                self.admin_site.admin_view(self.dashboard_view),
                name=f"{info[0]}_{info[1]}_changelist",
            ),
            path(
                "seed-preview/",
                self.admin_site.admin_view(self.seed_preview_view),
                name=f"{info[0]}_{info[1]}_seed_preview",
            ),
            path(
                "seed-execute/",
                self.admin_site.admin_view(self.seed_execute_view),
                name=f"{info[0]}_{info[1]}_seed_execute",
            ),
            path(
                "queue-retrieval/",
                self.admin_site.admin_view(self.queue_retrieval_view),
                name=f"{info[0]}_{info[1]}_queue_retrieval",
            ),
            path(
                "queue-pipeline/",
                self.admin_site.admin_view(self.queue_pipeline_view),
                name=f"{info[0]}_{info[1]}_queue_pipeline",
            ),
            path(
                "retry-failed/",
                self.admin_site.admin_view(self.retry_failed_view),
                name=f"{info[0]}_{info[1]}_retry_failed",
            ),
        ]

    # ── Shared helpers ──

    def _confirm_interstitial(self, request, action_label, action_url):
        """Show an interstitial confirmation page before destructive actions.

        This aligns with the shared confirmation UX pattern being introduced
        in issue #422 for ``crank/admin.py``: the operator must explicitly click
        a "Confirm" button on a dedicated page rather than relying on a hidden
        ``confirm=yes`` form field that is always present.
        """
        context = {
            **self.admin_site.each_context(request),
            "title": f"Confirm: {action_label}",
            "action_label": action_label,
            "action_url": action_url,
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/job_retrieval_confirm.html", context)

    def _dashboard_url(self):
        return reverse("admin:crank_jobretrievalops_changelist")

    def _admin_links(self):
        """Return admin links for related models."""
        return [
            {
                "label": "Job Source Catalog",
                "url": reverse("admin:crank_jobsourcecatalog_changelist"),
            },
            {
                "label": "Job Listings",
                "url": reverse("admin:crank_joblisting_changelist"),
            },
            {
                "label": "Crawl Runs",
                "url": reverse("admin:crank_crawlrun_changelist"),
            },
            {
                "label": "Agent Runs",
                "url": reverse("admin:crank_agentrun_changelist"),
            },
            {
                "label": "Employer Aliases",
                "url": reverse("admin:crank_employeralias_changelist"),
            },
            {
                "label": "Unresolved Employers",
                "url": reverse("admin:crank_unresolvedemployer_changelist"),
            },
            {
                "label": "Job Matches",
                "url": reverse("admin:crank_jobmatch_changelist"),
            },
            {
                "label": "Operational Change Audit",
                "url": reverse("admin:crank_operationalchangeaudit_changelist"),
            },
        ]

    # ── Views ──

    def dashboard_view(self, request):
        context = {
            **self.admin_site.each_context(request),
            "title": "Job Retrieval Operations",
            "counts": _aggregate_counts(),
            "gates": _readiness_gates(),
            "opts": self.model._meta,
            "admin_links": self._admin_links(),
        }
        return TemplateResponse(request, self.change_list_template, context)

    def seed_preview_view(self, request):
        """Dry-run preview of seed_job_sources without writing to the database.

        Renders actionable per-source preview rows so the operator can see
        which sources would be created, which already exist, and what their
        current approval/enabled state is.
        """
        preview = []
        existing_by_name = {
            obj.name: obj
            for obj in JobSourceCatalog.objects.filter(
                name__in=[entry["name"] for entry in SEED_SOURCES]
            )
        }
        for entry in SEED_SOURCES:
            host = _host(entry["base_url"])
            allowed = _is_allowed(host)
            existing = existing_by_name.get(entry["name"])
            preview.append(
                {
                    "name": entry["name"],
                    "adapter_key": entry["adapter_key"],
                    "base_url": entry["base_url"],
                    "host_allowed": allowed,
                    "exists": existing is not None,
                    "existing_enabled": existing.enabled if existing else None,
                    "existing_approval": existing.approval_state if existing else None,
                }
            )
        context = {
            **self.admin_site.each_context(request),
            "title": "Seed Preview (dry-run)",
            "preview": preview,
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/job_retrieval_seed_preview.html", context)

    def seed_execute_view(self, request):
        """Execute seed_job_sources with confirmation.

        Creates new sources with ``pending`` / ``enabled=False`` defaults, and
        preserves operator-set ``approval_state`` and ``enabled`` on existing
        rows so re-seeding never silently reclobbers a disabled or blocked source.
        The operator must explicitly approve and enable a source through the
        Job Source Catalog admin after verifying adapter registration and
        secret presence.
        """
        if not _confirm(request):
            return self._confirm_interstitial(
                request,
                "Execute Seed: Create/Update Curated Job Sources",
                reverse("admin:crank_jobretrievalops_seed_execute"),
            )

        created = updated = skipped = 0
        for entry in SEED_SOURCES:
            host = _host(entry["base_url"])
            if not _is_allowed(host):
                skipped += 1
                continue
            obj, created_flag = JobSourceCatalog.objects.get_or_create(
                name=entry["name"],
                defaults={
                    "adapter_key": entry["adapter_key"],
                    "base_url": entry["base_url"],
                    "approval_state": JobSourceCatalog.ApprovalState.PENDING,
                    "enabled": False,
                    "catalog_metadata": entry.get("catalog_metadata", {}),
                },
            )
            if created_flag:
                created += 1
            else:
                # Update structural fields only; preserve operator-set policy
                # fields (approval_state, enabled) on existing rows.
                obj.adapter_key = entry["adapter_key"]
                obj.base_url = entry["base_url"]
                obj.catalog_metadata = entry.get("catalog_metadata", {})
                obj.save(update_fields=["adapter_key", "base_url", "catalog_metadata", "modified"])
                updated += 1

        summary = f"Seed complete: {created} created, {updated} updated, {skipped} skipped."
        _audit(request, "seed_job_sources", new_value={"created": created, "updated": updated, "skipped": skipped})
        monitoring.record_event(
            "operational_change",
            {"action": "seed_job_sources", "confirmed": True},
        )
        self.message_user(request, summary, level=messages.SUCCESS)
        return redirect(self._dashboard_url())

    def _acquire_pipeline_slot(self, action, count_sources=False):
        """Atomically claim the job-pipeline slot or report why we must skip.

        The DB partial unique index ``unique_agentrun_active_per_type`` is the
        authoritative overlap guard: at most one active (RUNNING or PENDING)
        run per ``run_type`` may exist. We gate with ``select_for_update``
        first (best-effort; it only locks *existing* rows, so on an empty
        result set it locks nothing), then fall back to catching the
        ``IntegrityError`` from the constraint hit on a concurrent insert.
        Returns ``(skip_message, run, source_count)`` where ``run`` is
        ``None`` when the request was skipped.
        """
        skip_message = None
        run = None
        source_count = 0
        skip_reason = None
        # Compute source count outside the transaction to minimize lock
        # duration.
        if count_sources:
            source_count = JobSourceCatalog.objects.filter(
                approval_state=JobSourceCatalog.ApprovalState.APPROVED,
                enabled=True,
            ).count()
        try:
            with transaction.atomic():
                existing = AgentRun.objects.select_for_update().filter(
                    run_type=AgentRun.RunType.JOB_PIPELINE,
                    status__in=[
                        AgentRun.Status.RUNNING,
                        AgentRun.Status.PENDING,
                    ],
                ).first()
                if existing is not None:
                    skip_message = (
                        "Pipeline already active or queued "
                        f"(run {existing.correlation_id}). {action} skipped."
                    )
                    skip_reason = "overlap_existing"
                else:
                    try:
                        run = AgentRun.objects.create(
                            run_type=AgentRun.RunType.JOB_PIPELINE,
                            status=AgentRun.Status.PENDING,
                        )
                    except IntegrityError as create_exc:
                        # A concurrent request won the slot between our
                        # (empty) read and our insert. Inspect the error to
                        # confirm it is the overlap constraint (NIT-1):
                        # backends that expose the constraint name are checked
                        # directly; on backends that don't (MySQL), re-read for
                        # an active row after rollback and only treat as a skip
                        # if a winner exists.
                        msg = str(create_exc).lower()
                        if "unique_agentrun_active_per_type" in msg:
                            skip_reason = "overlap_constraint"
                        else:
                            # Re-read after rollback to confirm an active run
                            # exists; if none, re-raise the unexpected error.
                            active = AgentRun.objects.filter(
                                run_type=AgentRun.RunType.JOB_PIPELINE,
                                status__in=[
                                    AgentRun.Status.RUNNING,
                                    AgentRun.Status.PENDING,
                                ],
                            ).exists()
                            if active:
                                skip_reason = "overlap_constraint"
                            else:
                                raise
                        skip_message = (
                            f"Pipeline already active or queued. "
                            f"{action} skipped."
                        )
        except IntegrityError:
            # Non-create IntegrityError — re-raise instead of masking.
            raise
        if skip_reason:
            monitoring.record_event(
                "scheduled_run",
                {
                    "run_type": AgentRun.RunType.JOB_PIPELINE,
                    "status": "skipped",
                    "reason_code": skip_reason,
                    "action": action.lower(),
                },
            )
        return skip_message, run, source_count

    def queue_retrieval_view(self, request):
        """Queue retrieval for approved+enabled job sources.

        Overlap-safe: guards the RUNNING **and** PENDING pipeline slot with a
        ``select_for_update`` transaction plus a database partial unique
        constraint so concurrent double-submit requests cannot create duplicate
        runs.
        """
        if not _confirm(request):
            return self._confirm_interstitial(
                request,
                "Queue Retrieval for Approved+Enabled Sources",
                reverse("admin:crank_jobretrievalops_queue_retrieval"),
            )

        skip_message, run, count = self._acquire_pipeline_slot(
            "Queue", count_sources=True
        )
        if skip_message:
            self.message_user(request, skip_message, level=messages.WARNING)
            return redirect(self._dashboard_url())

        _audit(
            request,
            "queue_retrieval",
            new_value={"sources_count": count, "agent_run_id": run.pk},
        )
        monitoring.record_event(
            "operational_change",
            {"action": "queue_retrieval", "confirmed": True},
        )
        self.message_user(
            request,
            f"Retrieval queued for {count} approved+enabled sources (run {run.correlation_id}).",
            level=messages.SUCCESS,
        )
        return redirect(self._dashboard_url())

    def queue_pipeline_view(self, request):
        """Queue a bounded job pipeline run.

        Overlap-safe: guards the RUNNING **and** PENDING pipeline slot with a
        ``select_for_update`` transaction plus a database partial unique
        constraint so concurrent double-submit requests cannot create duplicate
        runs.
        """
        if not _confirm(request):
            return self._confirm_interstitial(
                request,
                "Queue Job Pipeline Run",
                reverse("admin:crank_jobretrievalops_queue_pipeline"),
            )

        skip_message, run, _ = self._acquire_pipeline_slot("Queue")
        if skip_message:
            self.message_user(request, skip_message, level=messages.WARNING)
            return redirect(self._dashboard_url())

        _audit(
            request,
            "queue_pipeline",
            new_value={"agent_run_id": run.pk},
        )
        monitoring.record_event(
            "operational_change",
            {"action": "queue_pipeline", "confirmed": True},
        )
        self.message_user(
            request,
            f"Pipeline run queued (run {run.correlation_id}).",
            level=messages.SUCCESS,
        )
        return redirect(self._dashboard_url())

    def retry_failed_view(self, request):
        """Retry the most recent eligible failed pipeline run.

        Overlap-safe: guards the RUNNING **and** PENDING pipeline slot and
        looks up the failed run to retry inside a ``select_for_update``
        transaction (locking the failed row serializes concurrent retries), and
        the database partial unique constraint is the authoritative guard that
        only one active run per type may exist.
        """
        if not _confirm(request):
            return self._confirm_interstitial(
                request,
                "Retry Eligible Failed Pipeline Run",
                reverse("admin:crank_jobretrievalops_retry_failed"),
            )

        failed = None
        skip_message = None
        skip_reason = None
        run = None
        try:
            with transaction.atomic():
                existing = AgentRun.objects.select_for_update().filter(
                    run_type=AgentRun.RunType.JOB_PIPELINE,
                    status__in=[
                        AgentRun.Status.RUNNING,
                        AgentRun.Status.PENDING,
                    ],
                ).first()
                if existing is not None:
                    skip_message = (
                        "Pipeline already active or queued "
                        f"(run {existing.correlation_id}). Retry skipped."
                    )
                    skip_reason = "overlap_existing"
                else:
                    failed = (
                        AgentRun.objects.select_for_update()
                        .filter(
                            run_type=AgentRun.RunType.JOB_PIPELINE,
                            status=AgentRun.Status.FAILED,
                        )
                        .order_by("-created", "-id")
                        .first()
                    )
                    if failed is None:
                        skip_message = "No eligible failed run to retry."
                    else:
                        try:
                            run = AgentRun.objects.create(
                                run_type=AgentRun.RunType.JOB_PIPELINE,
                                status=AgentRun.Status.PENDING,
                            )
                        except IntegrityError as create_exc:
                            # A concurrent request won the slot between our
                            # (empty) read and our insert. Inspect the error
                            # to confirm it is the overlap constraint (NIT-1):
                            msg = str(create_exc).lower()
                            if "unique_agentrun_active_per_type" in msg:
                                skip_reason = "overlap_constraint"
                            else:
                                active = AgentRun.objects.filter(
                                    run_type=AgentRun.RunType.JOB_PIPELINE,
                                    status__in=[
                                        AgentRun.Status.RUNNING,
                                        AgentRun.Status.PENDING,
                                    ],
                                ).exists()
                                if active:
                                    skip_reason = "overlap_constraint"
                                else:
                                    raise
                            skip_message = (
                                "Pipeline already active or queued. "
                                "Retry skipped."
                            )
        except IntegrityError:
            # Non-create IntegrityError (from the select or failed lookup).
            # Re-raise: these should not be silently swallowed.
            raise

        if skip_reason:
            monitoring.record_event(
                "scheduled_run",
                {
                    "run_type": AgentRun.RunType.JOB_PIPELINE,
                    "status": "skipped",
                    "reason_code": skip_reason,
                    "action": "retry_failed",
                },
            )
        if skip_message:
            self.message_user(request, skip_message, level=messages.WARNING)
            return redirect(self._dashboard_url())

        _audit(
            request,
            "retry_failed",
            old_value={
                "retried_run_id": failed.pk,
                "retried_correlation_id": str(failed.correlation_id),
            },
            new_value={"agent_run_id": run.pk},
        )
        monitoring.record_event(
            "operational_change",
            {"action": "retry_failed", "confirmed": True},
        )
        self.message_user(
            request,
            f"Retry queued as run {run.correlation_id} (retried from {failed.correlation_id}).",
            level=messages.SUCCESS,
        )
        return redirect(self._dashboard_url())
