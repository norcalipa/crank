# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Read-only end-to-end readiness snapshot for Job Retrieval Operations (#481).

Every derivation the staff dashboard shows lives here so the page, later
telemetry gauges, and tests share one definition. Nothing in this module writes
to the database, reaches the network, constructs an adapter, or returns a
credential value: outputs are bounded ints, ISO timestamps, booleans, short
enums, and sanitized/truncated text.

``STAGES`` keys are a frozen contract once merged; ``backlog()`` returns only
ints, ISO timestamps, booleans, and enums so it can feed gauges unchanged.
"""

from __future__ import annotations

import logging
import os
import traceback
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Min, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from crank.agents.jobs.base import validate_job_url
from crank.agents.jobs.registry import REGISTRY
from crank.models.agent_run import AgentRun
from crank.models.company_profile import CompanyProfileObservation
from crank.models.crawl_run import CrawlRun
from crank.models.employer import UnresolvedEmployer
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.preference import UserPreference
from crank.models.publication import PublicationEvent
from crank.services import inventory_health, match_recompute, match_results, monitoring, publication
from crank.services.agent_runs import sanitize_error
from crank.services.job_pipeline import COUNT_KEYS

logger = logging.getLogger(__name__)

RUNBOOK_BASE_URL = "https://github.com/norcalipa/crank/blob/main/docs/"

_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep

MET, UNMET, PENDING, ATTENTION, UNKNOWN = "met", "unmet", "pending", "attention", "unknown"
STATUSES = (MET, UNMET, PENDING, ATTENTION, UNKNOWN)

#: Ordered ``(key, label, runbook anchor)``. Keys are frozen once merged.
STAGES = (
    ("source_policy", "Approved and enabled source", "runbook-initial-crawl.md#step-1-seed-job-sources"),
    ("adapter", "Registered adapter and allowlisted URL", "runbook-initial-crawl.md#step-1-seed-job-sources"),
    ("credentials", "Source credentials present", "runbook-initial-crawl.md#step-3-set-the-firecrawl-api-key"),
    ("capability", "Job pipeline capability enabled", "runbook-initial-crawl.md#step-4-enable-capability-switches"),
    ("scheduler", "Recent pipeline run finished", "runbook-initial-crawl.md#step-7-unsuspend-cronjobs"),
    ("consumption", "Queued run consumed", "runbook-crawl-scheduling.md#queued-runs-are-consumed-issue-462"),
    ("inventory", "Committed listing inventory", "runbook-initial-crawl.md#step-6-verify-listing-counts"),
    ("employers", "Employers resolved", "runbook-initial-crawl.md#step-6-verify-listing-counts"),
    ("matches", "Current matches", "match-recompute.md#rollout-order"),
)
STAGE_KEYS = tuple(key for key, _label, _anchor in STAGES)

#: ``AgentRun.counts`` keys shown per stage of a completed run, derived from the
#: pipeline's own ``COUNT_KEYS`` with an "Other" group for keys no prefix claims, so a new counter is never dropped.
RUN_COUNT_GROUPS = (
    ("Sources", tuple(k for k in COUNT_KEYS if k.startswith("sources_"))),
    ("Listings", tuple(k for k in COUNT_KEYS if k.startswith("listings_"))),
    ("Employers", tuple(k for k in COUNT_KEYS if k.startswith("employers_"))),
    (
        "Matches",
        tuple(
            k
            for k in COUNT_KEYS
            if k.startswith(("users_", "matches_")) or k in ("stale_discarded", "duplicate_skipped")
        ),
    ),
)
_GROUPED_KEYS = {key for _label, keys in RUN_COUNT_GROUPS for key in keys}
RUN_COUNT_GROUPS += (
    ("Other", tuple(k for k in COUNT_KEYS if k not in _GROUPED_KEYS and k != "deadline_reached")),
)

_NAME_LIST_LIMIT = 5
_SOURCE_LIMIT = 500
_RUN_ERROR_LIMIT = 300
_SOURCE_ERROR_LIMIT = 200


def runbook_url(anchor):
    return RUNBOOK_BASE_URL + anchor


def format_age(seconds):
    """Coarse age such as ``45s``, ``12m``, ``3h`` or ``2d`` from whole seconds."""
    seconds = max(0, int(seconds))
    minutes = seconds // 60
    hours = minutes // 60
    days = hours // 24
    if days:
        return f"{days}d"
    if hours:
        return f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _iso(dt):
    return dt.isoformat() if dt else None


def _age_seconds(dt, now):
    return None if dt is None else max(0, int((now - dt).total_seconds()))


def setting_present(name):
    """Boolean presence of a setting/env value; the value itself never leaves."""
    value = getattr(settings, name, "")
    if not value:
        value = os.environ.get(name, "")
    return bool(str(value or "").strip())


def _admin(name, query=""):
    return reverse(f"admin:{name}") + query


def _names(names):
    names = sorted(names)
    shown = ", ".join(names[:_NAME_LIST_LIMIT])
    extra = len(names) - _NAME_LIST_LIMIT
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _freshness_hours():
    return max(1, inventory_health.freshness_hours() or 24)


class _Context:
    """Lazily computed, shared inputs so each query runs at most once."""

    def __init__(self, now):
        self.now = now
        self._cache = {}

    def _memo(self, key, factory):
        if key not in self._cache:
            self._cache[key] = factory()
        return self._cache[key]

    @property
    def source_counts(self):
        def build():
            return JobSourceCatalog.objects.aggregate(
                total=Count("pk"),
                approved=Count("pk", filter=Q(approval_state=JobSourceCatalog.ApprovalState.APPROVED)),
                blocked=Count("pk", filter=Q(approval_state=JobSourceCatalog.ApprovalState.BLOCKED)),
                enabled_any=Count("pk", filter=Q(enabled=True)),
                live=Count(
                    "pk",
                    filter=Q(approval_state=JobSourceCatalog.ApprovalState.APPROVED, enabled=True),
                ),
            )

        return self._memo("source_counts", build)

    @property
    def live_sources(self):
        """Approved-and-enabled sources: the only rows that can be retrieved."""

        def build():
            return list(
                JobSourceCatalog.objects.filter(
                    approval_state=JobSourceCatalog.ApprovalState.APPROVED, enabled=True
                )
                .only("name", "adapter_key", "base_url")
                .order_by("name")[:_SOURCE_LIMIT]
            )

        return self._memo("live_sources", build)

    @property
    def live_adapter_counts(self):
        """``{adapter_key: live source count}`` over every live source (one query, bounded by adapter count)."""

        def build():
            rows = (
                JobSourceCatalog.objects.filter(
                    approval_state=JobSourceCatalog.ApprovalState.APPROVED, enabled=True
                )
                .order_by()
                .values("adapter_key")
                .annotate(n=Count("pk"))
            )
            return {row["adapter_key"]: row["n"] for row in rows}

        return self._memo("live_adapter_counts", build)

    @property
    def listings(self):
        def build():
            live = Q(
                source__approval_state=JobSourceCatalog.ApprovalState.APPROVED,
                source__enabled=True,
            )
            return JobListing.objects.aggregate(
                active=Count("pk"),
                from_live_sources=Count("pk", filter=live),
                with_organization=Count("pk", filter=Q(organization__isnull=False)),
            )

        return self._memo("listings", build)

    @property
    def last_finished_run(self):
        def build():
            return (
                AgentRun.objects.filter(
                    run_type=AgentRun.RunType.JOB_PIPELINE,
                    finished_at__isnull=False,
                    status__in=[AgentRun.Status.SUCCEEDED, AgentRun.Status.FAILED],
                )
                .order_by("-finished_at", "-id")
                .first()
            )

        return self._memo("last_finished_run", build)

    @property
    def latest_run(self):
        def build():
            return (
                AgentRun.objects.filter(run_type=AgentRun.RunType.JOB_PIPELINE)
                .exclude(status=AgentRun.Status.SKIPPED)
                .order_by("-created", "-id")
                .first()
            )

        return self._memo("latest_run", build)

    @property
    def active_run(self):
        """Oldest RUNNING/PENDING pipeline run: the one that owns the slot."""

        def build():
            return (
                AgentRun.objects.filter(
                    run_type=AgentRun.RunType.JOB_PIPELINE,
                    status__in=[AgentRun.Status.RUNNING, AgentRun.Status.PENDING],
                )
                .order_by("id")
                .first()
            )

        return self._memo("active_run", build)

    @property
    def pending_count(self):
        return self._memo(
            "pending_count",
            lambda: AgentRun.objects.filter(
                run_type=AgentRun.RunType.JOB_PIPELINE, status=AgentRun.Status.PENDING
            ).count(),
        )

    @property
    def oldest_pending(self):
        return self._memo(
            "oldest_pending",
            lambda: AgentRun.objects.filter(
                run_type=AgentRun.RunType.JOB_PIPELINE, status=AgentRun.Status.PENDING
            )
            .order_by("created")
            .first(),
        )

    @property
    def capability_parts(self):
        return self._memo("capability_parts", capability_parts)

    @property
    def match_read_enabled(self):
        return self._memo("match_read_enabled", lambda: bool(match_results.read_enabled()))

    @property
    def recompute_enabled(self):
        return self._memo("recompute_enabled", lambda: bool(match_recompute.recompute_enabled()))

    @property
    def health(self):
        return self._memo("health", lambda: inventory_health.check_inventory_health(now=self.now))

    @property
    def unresolved_employers(self):
        return self._memo(
            "unresolved", lambda: UnresolvedEmployer.objects.filter(resolved=False).count()
        )

    @property
    def match_lag(self):
        return self._memo("match_lag", lambda: publication_match_lag_seconds(self.now))


def log_failure(what, exc):
    """Log the exception type and project call-site frames only: no values, no messages."""
    extracted = traceback.extract_tb(exc.__traceback__)
    own = [f for f in extracted if f.filename.startswith(_PACKAGE_DIR)] or extracted
    frames = " > ".join(f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in own[-6:])
    logger.warning("%s failed: %s [%s]", what, type(exc).__name__, frames)


def _plural(count, noun):
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _exist(count):
    return "exists" if count == 1 else "exist"


def _result(key, status, summary, remediation="", link=None, anchor=None, not_applicable=False):
    """Build one stage dict; ``link`` is ``(admin url name, query, label)``."""
    label, default_anchor = next((lbl, a) for k, lbl, a in STAGES if k == key)
    admin_name, admin_query, admin_label = link or ("", "", "")
    return {
        "key": key,
        "label": label,
        "status": status,
        "summary": summary,
        "remediation": remediation,
        "admin_url": _admin(admin_name, admin_query) if admin_name else "",
        "admin_label": admin_label,
        "runbook_url": runbook_url(anchor or default_anchor),
        "not_applicable": not_applicable,
    }


SOURCES_LINK = ("crank_jobsourcecatalog_changelist", "", "Job Source Catalog")


def _waiting(key, upstream):
    return _result(key, PENDING, f"Waiting on step {STAGE_KEYS.index(upstream) + 1}.")


def _stage_source_policy(ctx):
    counts = ctx.source_counts
    live = counts["live"]
    if live:
        return _result("source_policy", MET, f"{_plural(live, 'source')} approved and enabled in the database.")
    if not counts["total"]:
        remediation = "No job sources exist in the database. Preview and run the curated seed, then approve and enable a source."
    else:
        remediation = (
            f"{_plural(counts['total'], 'source')} {_exist(counts['total'])} but none is both approved and enabled "
            f"({counts['approved']} approved, {counts['blocked']} blocked, {counts['enabled_any']} enabled). "
            "Approve and enable one in the Job Source Catalog."
        )
    return _result(
        "source_policy",
        UNMET,
        "No source is approved and enabled in the database.",
        remediation,
        SOURCES_LINK,
    )


def _stage_adapter(ctx):
    if not ctx.live_sources:
        return _waiting("adapter", "source_policy")
    unregistered = [s.name for s in ctx.live_sources if REGISTRY.get(s.adapter_key) is None]
    unregistered_total = sum(n for key, n in ctx.live_adapter_counts.items() if REGISTRY.get(key) is None)
    unlisted = unregistered_total - len(unregistered)
    live_total = ctx.source_counts["live"]
    partial = live_total > len(ctx.live_sources)
    bad_url = []
    blocked = {}
    for source in ctx.live_sources:
        try:
            validate_job_url(source.base_url)
        except Exception:
            bad_url.append(source.name)
            continue
        adapter_cls = REGISTRY.get(source.adapter_key)
        if adapter_cls is not None:
            blockers = adapter_cls.startup_blockers(source)
            if blockers:
                blocked[source.name] = blockers
    if not unregistered_total and not bad_url and not blocked:
        if partial:
            return _result(
                "adapter",
                ATTENTION,
                f"Every live source has a registered adapter, but URL and startup checks covered only the first "
                f"{len(ctx.live_sources)} of {live_total} live sources.",
                "Review the remaining sources in the Job Source Catalog.",
                SOURCES_LINK,
            )
        return _result("adapter", MET, "Every live source has a registered adapter that can start, and an allowlisted HTTPS URL.")
    parts = []
    if unregistered_total:
        text = f"no registered adapter: {_names(unregistered)}" if unregistered else "no registered adapter"
        if unlisted > 0:
            text += f" ({_plural(unlisted, 'more source')} beyond the first {len(ctx.live_sources)} checked by name)"
        parts.append(text)
    if bad_url:
        parts.append(f"URL not allowlisted HTTPS: {_names(bad_url)}")
    if blocked:
        parts.append(
            "adapter will not start: "
            + "; ".join(f"{name} ({', '.join(issues)})" for name, issues in sorted(blocked.items())[:_NAME_LIST_LIMIT])
        )
    return _result(
        "adapter",
        UNMET,
        "; ".join(parts),
        "Correct the adapter key or base URL on the source row, turn on the named enable setting in deployment config, "
        f"or disable the source. Registered adapters: {', '.join(REGISTRY.keys())}.",
        SOURCES_LINK,
    )


def missing_settings(adapter_key):
    """Names (never values) of required settings that are empty for an adapter."""
    adapter_cls = REGISTRY.get(adapter_key)
    if adapter_cls is None:
        return None
    return [name for name in getattr(adapter_cls, "required_settings", ()) if not setting_present(name)]


def _stage_credentials(ctx):
    if not ctx.live_sources:
        return _waiting("credentials", "source_policy")
    counts = ctx.live_adapter_counts
    if any(REGISTRY.get(key) is None for key in counts):
        return _waiting("credentials", "adapter")
    missing_by_key = {key: names for key in counts if (names := missing_settings(key))}
    if not missing_by_key:
        return _result("credentials", MET, "Every live source's adapter has its required settings configured.")
    missing_total = sum(counts[key] for key in missing_by_key)
    missing = {s.name: missing_by_key[s.adapter_key] for s in ctx.live_sources if s.adapter_key in missing_by_key}
    if missing:
        detail = "; ".join(f"{name}: {', '.join(names)}" for name, names in sorted(missing.items())[:_NAME_LIST_LIMIT])
        if missing_total > len(missing):
            detail += f" (+{missing_total - len(missing)} more)"
    else:
        detail = "; ".join(f"{key}: {', '.join(names)}" for key, names in sorted(missing_by_key.items()))
    return _result(
        "credentials",
        UNMET,
        f"Missing settings for {_plural(missing_total, 'source')} — {detail}.",
        "Set the named environment variables in the deployment config (values are never shown here), then redeploy.",
    )


def capability_parts():
    """Booleans for each part of the three-part pipeline gate."""
    return {
        "agent_run_enabled": bool(getattr(settings, "AGENT_RUN_ENABLED", False)),
        "job_pipeline_enabled": bool(getattr(settings, "JOB_PIPELINE_ENABLED", False)),
        "switch_enabled": monitoring.capability_enabled("job_pipeline", default=True),
    }


def _stage_capability(ctx):
    from crank.capability import capability_report

    parts = ctx.capability_parts
    off = []
    if not parts["agent_run_enabled"]:
        off.append("AGENT_RUN_ENABLED is off")
    if not parts["job_pipeline_enabled"]:
        off.append("JOB_PIPELINE_ENABLED is off")
    if not parts["switch_enabled"]:
        off.append("the job_pipeline capability switch is disabled")
    issues = [] if off else [
        sanitize_error(issue, 200)
        for status in capability_report().capabilities
        if status.name == "job_pipeline"
        for issue in status.issues
    ]
    if not off and not issues:
        return _result("capability", MET, "AGENT_RUN_ENABLED, JOB_PIPELINE_ENABLED and the job_pipeline switch are all on.")
    detail = "; ".join(off + issues)
    return _result(
        "capability",
        UNMET,
        f"The pipeline will not run: {detail}.",
        "Turn on the named setting in deployment config, or re-enable the job_pipeline capability switch.",
        ("crank_capabilityswitch_changelist", "", "Capability Switches"),
    )


def _raw_counts(run):
    return run.counts if isinstance(run.counts, dict) else {}


def _hit_deadline(run):
    return _raw_counts(run).get("deadline_reached") is True


def _count(run, key):
    value = _raw_counts(run).get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _stage_scheduler(ctx):
    hours = _freshness_hours()
    run = ctx.last_finished_run
    if run is not None and ctx.now - run.finished_at <= timedelta(hours=hours):
        age = format_age(_age_seconds(run.finished_at, ctx.now))
        link = ("crank_agentrun_changelist", "?run_type__exact=job_pipeline", "Agent Runs")
        if _hit_deadline(run):
            deferred = _count(run, "sources_deferred")
            return _result(
                "scheduler",
                ATTENTION,
                f"A pipeline run finished {age} ago but stopped at its deadline"
                + (f" ({_plural(deferred, 'source')} deferred)." if deferred else "."),
                "Later sources or users were not processed. Check the run's counts; the remainder is handled by the next run that is consumed.",
                link,
            )
        return _result("scheduler", MET, f"A pipeline run finished {age} ago.", link=link)
    return _result(
        "scheduler",
        UNMET,
        f"No pipeline run finished in the last {hours}h. The job-pipeline CronJob may be suspended; "
        "the app cannot read CronJob state.",
        "Confirm the job-pipeline CronJob is unsuspended (kubectl), or queue a run from Actions below.",
        ("crank_agentrun_changelist", "?run_type__exact=job_pipeline", "Agent Runs"),
    )


def _stage_consumption(ctx):
    ttl = timedelta(seconds=int(getattr(settings, "AGENT_RUN_STALE_AFTER_SECONDS", 3600)))
    link = ("crank_agentrun_changelist", "?run_type__exact=job_pipeline", "Agent Runs")
    active = ctx.active_run
    if active is not None and active.status == AgentRun.Status.PENDING:
        if active.created and ctx.now - active.created > ttl:
            return _result(
                "consumption",
                UNMET,
                "A queued run has waited past its TTL without being consumed.",
                "Confirm the job-pipeline CronJob is unsuspended and the consumer is deployed; the run will be reclaimed as failed.",
                link,
            )
        return _result(
            "consumption",
            PENDING,
            "Queued — waiting for a consumer. The next pipeline tick or a manual run adopts it.",
            "No action yet; if it is not consumed before the TTL, check the CronJob.",
            link,
        )
    if active is not None:
        started = active.started_at or active.created
        if started and ctx.now - started > ttl:
            return _result(
                "consumption",
                UNMET,
                "A running pipeline run has not finished within its TTL and is likely stalled.",
                "Open the run; the next claim reclaims it as stale. Confirm the CronJob and consumer are healthy.",
                link,
            )
        return _result("consumption", MET, "A run has been claimed and is in progress.", link=link)
    run = ctx.latest_run
    if run is None:
        return _result("consumption", PENDING, "No pipeline run has been queued or consumed yet.", link=link)
    if run.status == AgentRun.Status.FAILED:
        reason = (run.error_summary or "").lower()
        kind = (
            "reclaimed as stale"
            if "stale run reclaimed" in reason
            else "expired in the queue"
            if "queued run reclaimed" in reason
            else "failed"
        )
        return _result(
            "consumption",
            UNMET,
            f"The most recent pipeline run {kind}.",
            "Inspect the run's sanitized summary, then retry from Actions once the cause is fixed.",
            link,
        )
    return _result("consumption", MET, "The most recent run was consumed and finished.", link=link)


def _stage_inventory(ctx):
    if not ctx.live_sources:
        return _waiting("inventory", "source_policy")
    link = ("crank_joblisting_changelist", "?status__exact=active", "Job Listings")
    live_listings = ctx.listings["from_live_sources"]
    health = ctx.health
    fresh = health["enabled_sources"] - health["stale_sources"]
    if live_listings == 0:
        return _result(
            "inventory",
            UNMET,
            "No active listings from approved and enabled sources.",
            "Run the first crawl batch, then check the Crawl Runs for the source outcome.",
            ("crank_crawlrun_changelist", "", "Crawl Runs"),
            anchor="runbook-initial-crawl.md#step-5-run-the-first-crawl-batch",
        )
    if fresh <= 0:
        return _result(
            "inventory",
            UNMET,
            f"{_plural(live_listings, 'active listing')} {_exist(live_listings)} but no source has succeeded within {_freshness_hours()}h.",
            "Check the per-source last success and failure reasons below; the crawl may be failing or not scheduled.",
            ("crank_crawlrun_changelist", "", "Crawl Runs"),
        )
    problems = []
    if health["stale_sources"]:
        problems.append(f"{health['stale_sources']} stale")
    if health["repeated_failure_sources"]:
        problems.append(f"{health['repeated_failure_sources']} repeatedly failing")
    if health["collapsed_sources"]:
        problems.append(f"{health['collapsed_sources']} collapsed to zero listings")
    if problems:
        return _result(
            "inventory",
            ATTENTION,
            f"{_plural(live_listings, 'active listing')}; sources needing attention: {', '.join(problems)}.",
            "Review the per-source table below.",
            link,
        )
    return _result("inventory", MET, f"{_plural(live_listings, 'active listing')} from {_plural(fresh, 'freshly crawled source')}.", link=link)


def _stage_employers(ctx):
    listings = ctx.listings
    if listings["active"] == 0:
        return _waiting("employers", "inventory")
    link = ("crank_unresolvedemployer_changelist", "?resolved__exact=0", "Unresolved Employers")
    if listings["with_organization"] == 0:
        return _result(
            "employers",
            UNMET,
            f"{_plural(listings['active'], 'active listing')} but none resolved to an organization.",
            "Add employer aliases or resolve the queued employers so listings can match.",
            link,
        )
    unresolved = ctx.unresolved_employers
    if unresolved:
        return _result(
            "employers",
            ATTENTION,
            f"{_plural(unresolved, 'unresolved employer')} awaiting review.",
            "Resolve or alias them in Unresolved Employers.",
            link,
        )
    return _result("employers", MET, "Listings resolve to organizations and no employers await review.", link=link)


def _stage_matches(ctx):
    link = ("crank_jobmatch_changelist", "", "Job Matches")
    if ctx.match_read_enabled:
        counts = match_recompute.pending_counts(0)
        dirty = counts["preference_dirty"]
        lag = ctx.match_lag
        configured = int(getattr(settings, "MATCH_RECOMPUTE_MAX_AGE_HOURS", 24))
        # 0 disables the age backstop; the lag limit then falls back to the default.
        max_hours = configured if configured > 0 else 24
        tier_names = (
            "preference_dirty",
            "generation_dirty_data_stale",
            "generation_dirty_version_mismatch",
            "generation_dirty_interrupted",
        )
        tiers = ", ".join(
            f"{name.replace('generation_dirty_', '').replace('_', ' ')}: {counts[name]}"
            for name in tier_names
            if counts[name]
        )
        refresh = counts["generation_dirty_age_stale"]
        refresh_note = ""
        if refresh:
            drain = (
                "the recompute drain handles it"
                if ctx.recompute_enabled
                else "paused while MATCH_RECOMPUTE_ENABLED is off"
            )
            refresh_note = (
                f" {_plural(refresh, 'user')} due for the periodic refresh (older than {configured}h; {drain})."
            )
        if dirty == 0 and lag <= max_hours * 3600 and not tiers:
            return _result(
                "matches",
                MET,
                f"No user has preference changes pending, and no user's committed matches are stale, mismatched or interrupted; publication-to-match lag is within {max_hours}h."
                + refresh_note,
                link=link,
            )
        if dirty == 0 and lag <= max_hours * 3600:
            return _result(
                "matches",
                ATTENTION,
                f"Some users' committed matches are not current ({tiers}); publication-to-match lag {format_age(lag)}."
                + refresh_note,
                "Run the recompute drain so those users are republished; see the rollout order.",
                link,
            )
        return _result(
            "matches",
            UNMET,
            f"Committed matches are behind ({tiers or 'none pending'}); publication-to-match lag {format_age(lag)}."
            + refresh_note,
            "Enable MATCH_RECOMPUTE_ENABLED and run the recompute drain; see the rollout order.",
            link,
        )
    has_matches = JobMatch.objects.exists()
    run = ctx.last_finished_run
    counted = run is not None and "users_total" in _raw_counts(run)
    if has_matches or (counted and _count(run, "users_total") == 0) or (
        not counted and not UserPreference.objects.filter(user__is_active=True).exists()
    ):
        return _result(
            "matches",
            MET,
            "Live reads (committed generations not served): "
            + ("matches exist." if has_matches else "no user has active preferences yet."),
            link=link,
            not_applicable=not has_matches,
        )
    if counted:
        return _result(
            "matches",
            UNMET,
            "Live reads (committed generations not served): the last pipeline run processed "
            f"{_plural(_count(run, 'users_total'), 'user')} but persisted no matches.",
            "Check that run's counts and the users' preferences; no matches exist yet.",
            link,
        )
    return _result(
        "matches",
        UNMET,
        "Live reads (committed generations not served): users have preferences but no matches exist.",
        "Run the job pipeline so matches are persisted for users with preferences.",
        link,
    )


_STAGE_FUNCS = {
    "source_policy": _stage_source_policy,
    "adapter": _stage_adapter,
    "credentials": _stage_credentials,
    "capability": _stage_capability,
    "scheduler": _stage_scheduler,
    "consumption": _stage_consumption,
    "inventory": _stage_inventory,
    "employers": _stage_employers,
    "matches": _stage_matches,
}


def snapshot(now=None):
    """Shared memo so one page render issues each underlying query once."""
    return _Context(now or timezone.now())


def readiness(now=None, ctx=None):
    """Ordered stage results plus the single next step an operator should act on."""
    ctx = ctx or snapshot(now)
    stages = []
    for key in STAGE_KEYS:
        try:
            stage = _STAGE_FUNCS[key](ctx)
        except Exception as exc:
            log_failure(f"operations readiness stage {key}", exc)
            stage = _result(key, UNKNOWN, "Could not compute — see server logs.")
        stage["position"] = len(stages) + 1
        stages.append(stage)

    # The callout names the first UNMET stage: a blocker is never displaced by a
    # warning. Only when nothing is unmet does the first attention, pending
    # (waiting on upstream) or unknown stage take over. Attention is never "met".
    next_step = None
    for wanted in (UNMET, ATTENTION, PENDING, UNKNOWN):
        next_step = next((s for s in stages if s["status"] == wanted), None)
        if next_step is not None:
            break
    return {
        "stages": stages,
        "next_step": next_step,
        "all_met": all(s["status"] == MET for s in stages),
        "met_count": sum(1 for s in stages if s["status"] == MET),
        "unmet_count": sum(1 for s in stages if s["status"] == UNMET),
        "attention_count": sum(1 for s in stages if s["status"] == ATTENTION),
        "pending_count": sum(1 for s in stages if s["status"] == PENDING),
        "unknown_count": sum(1 for s in stages if s["status"] == UNKNOWN),
    }


def publication_match_lag_seconds(now=None):
    """Age of the oldest event newer than the lowest committed data revision.

    Only users with a ``UserPreference`` count: the drain never recomputes the rest.

    ``0`` when nobody has a generation or every generation reflects the
    watermark. Events carry no per-user link, so this is a fleet-level bound.
    """
    now = now or timezone.now()
    states = MatchResultState.objects.filter(current_generation__isnull=False).exclude(
        user__preferences__isnull=True
    )
    if not states.exists():
        return 0
    min_revision = states.aggregate(m=Min(Coalesce("data_revision", 0)))["m"] or 0
    created = (
        PublicationEvent.objects.filter(id__gt=min_revision).order_by("id").values_list("created_at", flat=True).first()
    )
    return _age_seconds(created, now) or 0


def outbox_backlog(now=None):
    now = now or timezone.now()
    pending = PublicationEvent.objects.filter(processed_at__isnull=True)
    oldest = pending.order_by("id").values_list("created_at", flat=True).first()
    return {
        "pending": pending.count(),
        "oldest_pending_at": _iso(oldest),
        "oldest_pending_age_seconds": _age_seconds(oldest, now),
        "consumer_enabled": bool(publication.consumer_enabled()),
    }


def backlog(now=None, ctx=None):
    """Bounded backlog sizes; ints, ISO timestamps, booleans only."""
    ctx = ctx or snapshot(now)
    now = ctx.now
    observations = CompanyProfileObservation.objects.aggregate(
        pending=Count("pk", filter=Q(status=CompanyProfileObservation.Status.PENDING)),
        conflicted=Count("pk", filter=Q(status=CompanyProfileObservation.Status.CONFLICTED)),
    )
    without_generation = UserPreference.objects.filter(
        Q(user__match_result_state__isnull=True) | Q(user__match_result_state__current_generation__isnull=True)
    ).count()
    return {
        "employers": {"unresolved": ctx.unresolved_employers},
        "review": {
            "observations_pending": observations["pending"],
            "observations_conflicted": observations["conflicted"],
        },
        "outbox": outbox_backlog(now),
        "matches": {
            "lag_seconds": ctx.match_lag,
            "users_without_generation": without_generation,
            "recompute_enabled": ctx.recompute_enabled,
            "read_enabled": ctx.match_read_enabled,
        },
    }


def source_rows(limit=50, now=None, ctx=None):
    """Per-source table rows (ordered by name, capped) with sanitized outcomes."""
    ctx = ctx or snapshot(now)
    now = ctx.now
    latest_run = CrawlRun.objects.filter(job_source=OuterRef("pk")).order_by("-started_at", "-id")
    total = ctx.source_counts["total"]
    queryset = (
        JobSourceCatalog.objects.annotate(
            latest_outcome=Subquery(latest_run.values("outcome")[:1]),
            latest_error=Subquery(latest_run.values("error_summary")[:1]),
        )
        .order_by("name")[:limit]
    )
    rows = []
    for source in queryset:
        missing = missing_settings(source.adapter_key)
        rows.append(
            {
                "name": source.name,
                "adapter_key": source.adapter_key,
                "approval_state": source.approval_state,
                "enabled": bool(source.enabled),
                "adapter_registered": missing is not None,
                "credentials_present": None if missing is None else not missing,
                "last_success_at": _iso(source.last_crawl_at),
                "last_success_age": format_age(_age_seconds(source.last_crawl_at, now)) if source.last_crawl_at else "",
                "last_attempt_at": _iso(source.last_attempt_at),
                "last_attempt_age": format_age(_age_seconds(source.last_attempt_at, now)) if source.last_attempt_at else "",
                "consecutive_failures": int(source.consecutive_failures),
                "latest_outcome": source.latest_outcome or "",
                "latest_error": sanitize_error(source.latest_error, _SOURCE_ERROR_LIMIT),
                "admin_url": reverse("admin:crank_jobsourcecatalog_change", args=[source.pk]),
            }
        )
    return {"rows": rows, "total": total, "shown": len(rows), "truncated": total > len(rows)}


def _run_summary(run, now):
    if run is None:
        return None
    started_age = _age_seconds(run.started_at, now)
    finished_age = _age_seconds(run.finished_at, now)
    created_age = _age_seconds(run.created, now)
    duration = (
        int((run.finished_at - run.started_at).total_seconds()) if run.started_at and run.finished_at else None
    )
    return {
        "id": run.pk,
        "status": run.status,
        "queued_only": run.status == AgentRun.Status.PENDING,
        "created_at": _iso(run.created),
        "created_age": format_age(created_age) if created_age is not None else "",
        "started_at": _iso(run.started_at),
        "started_age": format_age(started_age) if started_age is not None else "",
        "finished_at": _iso(run.finished_at),
        "finished_age": format_age(finished_age) if finished_age is not None else "",
        "duration": format_age(duration) if duration is not None else "",
        "error_summary": sanitize_error(run.error_summary, _RUN_ERROR_LIMIT),
        "run_url": reverse("admin:crank_agentrun_change", args=[run.pk]),
    }


def run_progress(now=None, ctx=None):
    """Latest run state (queued is never shown as work) and the last completed run."""
    ctx = ctx or snapshot(now)
    now = ctx.now
    latest = _run_summary(ctx.latest_run, now)
    completed_run = ctx.last_finished_run
    completed = _run_summary(completed_run, now)
    if completed is not None:
        raw = _raw_counts(completed_run)
        completed["deadline_reached"] = _hit_deadline(completed_run)
        completed["sources_deferred"] = _count(completed_run, "sources_deferred")
        completed["stages"] = [
            {
                "label": label,
                "counts": [
                    {"key": key, "label": key.removeprefix(label.lower() + "_").replace("_", " "), "value": int(raw[key])}
                    for key in keys
                    if isinstance(raw.get(key), (int, float)) and not isinstance(raw.get(key), bool)
                ],
            }
            for label, keys in RUN_COUNT_GROUPS
        ]
        completed["links"] = [
            {"label": "Agent run", "url": completed["run_url"]},
            {"label": "Crawl runs for this run", "url": _admin("crank_crawlrun_changelist", f"?agent_run__id__exact={completed['id']}")},
            {"label": "Active job listings", "url": _admin("crank_joblisting_changelist", "?status__exact=active")},
            {"label": "Job matches", "url": _admin("crank_jobmatch_changelist", "?dismissed__exact=0")},
        ]
    return {"latest": latest, "completed": completed}


__all__ = [
    "snapshot",
    "RUNBOOK_BASE_URL",
    "STAGES",
    "STAGE_KEYS",
    "STATUSES",
    "backlog",
    "capability_parts",
    "format_age",
    "missing_settings",
    "outbox_backlog",
    "publication_match_lag_seconds",
    "readiness",
    "run_progress",
    "setting_present",
    "source_rows",
]
