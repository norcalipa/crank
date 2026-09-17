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
from datetime import timedelta
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
from crank.services import agent_runs, monitoring

logger = logging.getLogger(__name__)

# How stale (in hours) a listing must be before we consider it stale.
_STALE_HOURS = int(getattr(settings, "JOB_LISTING_STALE_HOURS", 24 * 7))


# ── Operator-facing status presentation (visual critique round 1) ──
# Stable mappings so the template's badge classes are contract-testable.
RUN_STATUS_LABELS = {
    "pending": "Queued",
    "running": "Running",
    "succeeded": "Succeeded",
    "failed": "Failed",
    "skipped": "Skipped",
}
RUN_STATUS_TONES = {
    "pending": "info",
    "running": "info",
    "succeeded": "success",
    "failed": "danger",
    "skipped": "warning",
}
RUN_STATUS_ICONS = {
    "pending": "⏳",
    "running": "▶",
    "succeeded": "✓",
    "failed": "✖",
    "skipped": "↻",
}
PIPELINE_STATE_TONES = {
    "idle": "success",
    "queued": "info",
    "claimed": "info",
    "conflict": "warning",
    "reclaimed": "warning",
    "expired": "danger",
    "failed": "danger",
}
PIPELINE_STATE_ICONS = {
    "idle": "✓",
    "queued": "⏳",
    "claimed": "▶",
    "conflict": "⚠",
    "reclaimed": "↻",
    "expired": "✖",
    "failed": "✖",
}


def _relative_time(dt, now=None):
    """Friendly relative time like ``12m ago`` (primary metadata display)."""
    if dt is None:
        return ""
    now = now or timezone.now()
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 45:
        return "just now"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def _duration(seconds):
    """Compact duration like ``2h 59m`` for countdown/age display."""
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def _abbrev_id(value):
    """Abbreviate a long correlation id like ``99a1f02e…2f15b``."""
    text = str(value)
    if len(text) <= 16:
        return text
    return f"{text[:8]}…{text[-5:]}"


def _pipeline_ownership_state():
    """Derive the operator-facing pipeline ownership & queue state.

    One consolidated, truthful presentation (issue #462 visual critique):
    who owns the run slot, when ownership began, what the queue holds, what
    happens next, and explicit conflict/reclaim/expired presentations. All
    derivations come from persisted ``AgentRun`` rows — never guesses.
    """
    now = timezone.now()
    stale_after = timedelta(
        seconds=int(getattr(settings, "AGENT_RUN_STALE_AFTER_SECONDS", 3600))
    )
    runs = AgentRun.objects.filter(run_type=AgentRun.RunType.JOB_PIPELINE)
    active = runs.filter(
        status__in=[AgentRun.Status.RUNNING, AgentRun.Status.PENDING]
    ).order_by("id").first()
    latest = runs.order_by("-created", "-id").first()

    pending_qs = runs.filter(status=AgentRun.Status.PENDING)
    pending_count = pending_qs.count()
    oldest_pending = pending_qs.order_by("created").first()

    # A skip attempt recorded after the current holder claimed the slot is
    # persisted evidence of an ownership conflict (a second invocation tried
    # to claim while this run holds the slot).
    conflict_skip = None
    if active is not None:
        conflict_skip = (
            runs.filter(
                status=AgentRun.Status.SKIPPED, created__gte=active.created
            )
            .order_by("-id")
            .first()
        )

    state = {
        "state": "idle",
        "tone": PIPELINE_STATE_TONES["idle"],
        "icon": PIPELINE_STATE_ICONS["idle"],
        "label": "Idle — no active or queued pipeline run",
        "explanation": (
            "Nothing is claimed or queued. The next scheduled pipeline tick "
            "(or a manual run) will pick up approved+enabled sources."
        ),
        "blocking": False,
        "owner": None,
        "owned_since_relative": None,
        "owned_since_iso": None,
        "queue_count": pending_count,
        "oldest_wait_relative": None,
        "oldest_reclaim_remaining": None,
        "oldest_correlation_id": None,
        "oldest_correlation_abbrev": None,
        "consumption": "Not running",
        "next_consumer": "Next scheduled pipeline tick or a manual run_job_pipeline invocation",
        "next_action": None,
        "alert_short": None,
        "correlation_id": None,
        "correlation_abbrev": None,
        "started_iso": None,
        "created_iso": None,
        "progress": None,
    }

    if oldest_pending is not None and oldest_pending.created is not None:
        age = max(0, int((now - oldest_pending.created).total_seconds()))
        state["oldest_wait_relative"] = _duration(age)
        state["oldest_reclaim_remaining"] = _duration(
            (stale_after - timedelta(seconds=age)).total_seconds()
        )
        state["oldest_correlation_id"] = str(oldest_pending.correlation_id)
        state["oldest_correlation_abbrev"] = _abbrev_id(
            oldest_pending.correlation_id
        )

    def _queue_suffix():
        if pending_count and state["oldest_wait_relative"]:
            return (
                f"Queue: {pending_count} run(s) waiting; oldest queued "
                f"{state['oldest_wait_relative']} ago."
            )
        return ""

    if active is not None:
        state["correlation_id"] = str(active.correlation_id)
        state["correlation_abbrev"] = _abbrev_id(active.correlation_id)
        state["created_iso"] = active.created.isoformat() if active.created else None
        state["started_iso"] = (
            active.started_at.isoformat() if active.started_at else None
        )
        if active.status == AgentRun.Status.PENDING:
            state.update(
                state="queued",
                tone=PIPELINE_STATE_TONES["queued"],
                icon=PIPELINE_STATE_ICONS["queued"],
                label="Queued — waiting for pipeline consumer",
                explanation=(
                    "The run is queued but not yet claimed. It is consumed by "
                    "the next scheduled pipeline tick or a manual "
                    "run_job_pipeline invocation. If no consumer adopts it "
                    f"within the staleness TTL (~{state['oldest_reclaim_remaining']} "
                    "remaining), it is reclaimed as failed."
                ),
                owner="Unclaimed — awaiting consumer",
                consumption="Waiting for consumer",
                next_consumer=(
                    "Next scheduled pipeline tick or a manual run_job_pipeline "
                    "invocation"
                ),
                next_action=(
                    "No action needed yet; if the TTL expires, queue the run again "
                    "after confirming the pipeline CronJob is unsuspended."
                ),
            )
        else:  # RUNNING
            adopted = bool(
                active.created
                and active.started_at
                and (active.started_at - active.created).total_seconds() > 5
            )
            progress = None
            if isinstance(active.counts, dict) and active.counts:
                progress = ", ".join(
                    f"{key.replace('items_', '').replace('_', ' ')}: {value}"
                    for key, value in sorted(active.counts.items())
                    if isinstance(value, (int, float, bool))
                )
            state.update(
                tone=PIPELINE_STATE_TONES["claimed"],
                icon=PIPELINE_STATE_ICONS["claimed"],
                owner="Job pipeline worker (claimed via the run-type slot)",
                owned_since_relative=_relative_time(active.started_at, now),
                consumption=(
                    f"Running — {progress}" if progress else "Running — no progress counters recorded yet"
                ),
                next_consumer="The running consumer finalizes this run (success/failure)",
            )
            if conflict_skip is not None:
                state.update(
                    state="conflict",
                    tone=PIPELINE_STATE_TONES["conflict"],
                    icon=PIPELINE_STATE_ICONS["conflict"],
                    label="Ownership conflict — another invocation was skipped",
                    explanation=(
                        "Another invocation attempted to claim the run slot while "
                        "this run holds it and was recorded as skipped. The current "
                        "owner keeps the slot; no operator action is required unless "
                        "the run goes stale."
                    ),
                    blocking=True,
                    alert_short=(
                        "Blocking status: a second consumer attempted to claim "
                        "this run's slot and was recorded as skipped."
                    ),
                    next_action=(
                        "Wait for the current owner to finalize. If it goes stale, "
                        "the next consumer reclaims the slot and retries."
                    ),
                )
            else:
                state.update(
                    state="claimed",
                    label="Claimed — owned by the Job pipeline worker",
                    explanation=(
                        "The run is owned by the deployed pipeline consumer"
                        + (
                            f" since {state['owned_since_relative']}"
                            if state["owned_since_relative"]
                            else ""
                        )
                        + (
                            " (adopted from the queue, correlation id preserved)."
                            if adopted
                            else "."
                        )
                    ),
                )
    else:
        reclaim_summary = (latest.error_summary or "") if latest else ""
        if latest is not None and latest.status == AgentRun.Status.FAILED:
            summary_lower = reclaim_summary.lower()
            state["correlation_id"] = str(latest.correlation_id)
            state["correlation_abbrev"] = _abbrev_id(latest.correlation_id)
            state["created_iso"] = latest.created.isoformat() if latest.created else None
            if "stale run reclaimed" in summary_lower:
                state.update(
                    state="reclaimed",
                    tone=PIPELINE_STATE_TONES["reclaimed"],
                    icon=PIPELINE_STATE_ICONS["reclaimed"],
                    label="Reclaimed — stale owner released",
                    explanation=(
                        "The previous owner was stale (started but never finalized, "
                        "likely a crash) and its slot was reclaimed. Queued work "
                        "will be retried by the next consumer."
                        + (f" {_queue_suffix()}" if pending_count else "")
                    ),
                    next_action=(
                        "Verify the pipeline CronJob is running; the next tick retries "
                        "automatically."
                    ),
                    consumption="Previous owner reclaimed",
                )
            elif "queued run reclaimed" in summary_lower:
                state.update(
                    state="expired",
                    tone=PIPELINE_STATE_TONES["expired"],
                    icon=PIPELINE_STATE_ICONS["expired"],
                    label="Failed — queued but never consumed within TTL",
                    explanation=(
                        "A queued run was never adopted by a consumer before the "
                        "staleness TTL expired and was reclaimed as failed."
                        + (f" {_queue_suffix()}" if pending_count else "")
                    ),
                    next_action=(
                        "Confirm the pipeline CronJob is unsuspended and the consumer "
                        "is deployed, then queue the run again."
                    ),
                    consumption="Timed out in queue",
                )
            else:
                state.update(
                    state="failed",
                    tone=PIPELINE_STATE_TONES["failed"],
                    icon=PIPELINE_STATE_ICONS["failed"],
                    label="Failed — last pipeline run failed",
                    explanation=(
                        "The most recent pipeline run failed."
                        + (f" {_queue_suffix()}" if pending_count else "")
                    ),
                    next_action="Inspect the sanitized summary below, then retry from Actions.",
                    consumption="Last run failed",
                    progress=reclaim_summary[:300] or None,
                )

    return state


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
            "label": RUN_STATUS_LABELS.get(latest_run.status, latest_run.status),
            "tone": RUN_STATUS_TONES.get(latest_run.status, "info"),
            "icon": RUN_STATUS_ICONS.get(latest_run.status, "•"),
            "created": latest_run.created.isoformat() if latest_run.created else None,
            "created_relative": _relative_time(latest_run.created),
            "correlation_id": str(latest_run.correlation_id),
            "correlation_abbrev": _abbrev_id(latest_run.correlation_id),
            "error_summary": (latest_run.error_summary or "")[:300],
        }

    # Queued (PENDING) pipeline runs awaiting a consumer (issue #462). A
    # queued run is not proof of work: surface the count and the age of the
    # oldest queued row so staff can see missing consumption instead of
    # indefinite pending work.
    pending_qs = AgentRun.objects.filter(
        run_type=AgentRun.RunType.JOB_PIPELINE,
        status=AgentRun.Status.PENDING,
    ).order_by("created")
    pending_count = pending_qs.count()
    pending_run_info = None
    oldest_pending = pending_qs.first()
    if oldest_pending is not None and oldest_pending.created is not None:
        age_seconds = max(
            0, int((timezone.now() - oldest_pending.created).total_seconds())
        )
        hours, remainder = divmod(age_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            age_display = f"{hours}h {minutes}m"
        elif minutes:
            age_display = f"{minutes}m {seconds}s"
        else:
            age_display = f"{seconds}s"
        pending_run_info = {
            "count": pending_count,
            "oldest_age_seconds": age_seconds,
            "oldest_age_display": age_display,
            "oldest_age_relative": _relative_time(oldest_pending.created),
            "oldest_correlation_id": str(oldest_pending.correlation_id),
            "oldest_correlation_abbrev": _abbrev_id(oldest_pending.correlation_id),
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
        "pending_run": pending_run_info,
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
        status__in=[AgentRun.Status.RUNNING, AgentRun.Status.PENDING],
    ).order_by("id").first()

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
            "pipeline": _pipeline_ownership_state(),
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
            # Persist the skip as a SKIPPED AgentRun row so the dashboard can
            # present a truthful ownership-conflict state ("another invocation
            # was skipped while this run holds the slot"), not just a flash
            # message. Bounded: only rows of status SKIPPED are created (never
            # counted by the active-run overlap guard).
            try:
                agent_runs.record_skipped(
                    AgentRun.RunType.JOB_PIPELINE, reason=skip_reason
                )
            except Exception:  # pragma: no cover - defensive
                logger.warning("failed to record overlap skip row", exc_info=True)
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
            try:
                agent_runs.record_skipped(
                    AgentRun.RunType.JOB_PIPELINE, reason=skip_reason
                )
            except Exception:  # pragma: no cover - defensive
                logger.warning("failed to record overlap skip row", exc_info=True)
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
