# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Emit a single, reproducible deployment/data readiness baseline record (issue #455).

The command is a thin composition of the existing safe helpers:

- ``crank.release``: git SHA / image identifier (``SOURCE_VERSION`` et al.),
  frontend webpack build id, bounded migration summary, selected job-search
  provider token;
- ``crank.capability``: the non-secret capability configuration report
  (secret presence is reduced to booleans; values are never serialized);
- ``crank.services.inventory_health``: bounded, read-only inventory signals;
- the operations dashboard's safe adapter/credential/pipeline/scheduler
  readiness gates, so an unmet gate is recorded even when the matching
  capability is disabled (and therefore considered OK by the capability
  report);
- the exact per-app migration leaf identifiers (applied and pending, bounded)
  and the staging fixture-set revision, so the record names the deployed
  schema revision and the loaded fixture set rather than aggregate counts;
- latest ``AgentRun`` per run type with its terminal status and a sanitized
  error summary;
- source counts mirroring ``crank.admin_dashboard._aggregate_counts()``.

The record never contains secret values: configured secret settings are
scrubbed from any free-text field before serialization, and the helpers above
already reduce secrets to presence booleans. Output is staff-only evidence
(see ``docs/deployment-baseline-2026-09.md``), not a public contract.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from crank.capability import capability_report
from crank.models.agent_run import AgentRun
from crank.models.job import JobSourceCatalog
from crank.models.organization import Organization
from crank.release import (
    UNKNOWN,
    _safe_job_search_provider,
    frontend_build_id,
    git_sha,
    migration_status_summary,
)
from crank.services.inventory_health import check_inventory_health

#: Settings that carry secret values and must never reach the baseline output.
_SECRET_SETTING_NAMES = (
    "SECRET_KEY",
    "LLM_API_KEY",
    "USAJOBS_AUTH_KEY",
    "FIRECRAWL_API_KEY",
    "YELP_API_KEY",
)

#: Bound on the number of per-app migration leaf identifiers emitted per list.
#: Leaf counts grow with the app count, not the migration count; the bound is
#: defense-in-depth so the record stays bounded no matter how apps grow.
_MIGRATION_LEAF_BOUND = 25

#: Error summaries are already bounded/sanitized at write time; this is
#: defense-in-depth for the baseline record itself.
_ERROR_SUMMARY_MAX_CHARS = 500

#: AgentRun statuses that represent a completed (terminal) outcome.
_TERMINAL_RUN_STATUSES = frozenset(
    {AgentRun.Status.SUCCEEDED, AgentRun.Status.FAILED, AgentRun.Status.SKIPPED}
)


def _configured_secrets() -> list[str]:
    """Return every non-empty configured secret value, longest first.

    The guarantee is unconditional: there is no minimum length below which a
    configured value may appear in the output, so short staging/test values
    are scrubbed just like production ones. Longest-first ordering (with
    duplicates removed) makes overlapping values scrub correctly (e.g.
    ``abc`` and ``abcdef``).
    """
    values = {
        str(getattr(settings, name, "") or "")
        for name in _SECRET_SETTING_NAMES
    }
    values.discard("")
    return sorted(values, key=len, reverse=True)


def _scrub(text: object) -> str:
    """Redact configured secret values from free text and bound its length."""
    text = str(text or "")
    for secret in _configured_secrets():
        if secret in text:
            text = text.replace(secret, "[redacted]")
    return text[:_ERROR_SUMMARY_MAX_CHARS]


def migration_leaves() -> dict:
    """Return bounded, exact per-app migration leaf identifiers.

    ``migration_status_summary()`` is aggregate-only; this names the exact
    deployed revision (the applied per-app leaves) and the exact pending
    leaves so the record can say which schema the database actually has.
    Both lists are bounded by ``_MIGRATION_LEAF_BOUND``.
    """
    try:
        executor = MigrationExecutor(connection)
        applied = executor.loader.applied_migrations
        leaves = executor.loader.graph.leaf_nodes()
        applied_leaves = sorted(
            f"{app}.{name}" for app, name in leaves if (app, name) in applied
        )
        pending_leaves = sorted(
            f"{app}.{name}" for app, name in leaves if (app, name) not in applied
        )
    except Exception:  # noqa: BLE001 - fail closed on any DB/loader error
        return {
            "applied": None,
            "applied_count": None,
            "pending": None,
            "pending_count": None,
            "truncated": False,
            "status": "error",
        }
    return {
        "applied": applied_leaves[:_MIGRATION_LEAF_BOUND],
        "applied_count": len(applied_leaves),
        "pending": pending_leaves[:_MIGRATION_LEAF_BOUND],
        "pending_count": len(pending_leaves),
        "truncated": (
            len(applied_leaves) > _MIGRATION_LEAF_BOUND
            or len(pending_leaves) > _MIGRATION_LEAF_BOUND
        ),
        "status": "ok",
    }


def fixture_set() -> dict:
    """Report whether the staging fixture set is loaded, and its revision.

    Presence is detected via the fixture set's unique marker rows (the fixture
    target organization or the fixture job source); the revision is the
    ``FIXTURE_REVISION`` constant owned by ``seed_staging_baseline``, bumped
    whenever the fixture set changes.
    """
    from crank.management.commands.seed_staging_baseline import (
        FIXTURE_REVISION,
        FIXTURE_SOURCE_NAME,
        TARGET_ORG_NAME,
    )

    present = (
        Organization.objects.filter(name=TARGET_ORG_NAME).exists()
        or JobSourceCatalog.objects.filter(name=FIXTURE_SOURCE_NAME).exists()
    )
    return {
        "revision": FIXTURE_REVISION if present else None,
        "present": present,
    }


def readiness_gates() -> dict:
    """Mirror the operations dashboard's safe adapter/credential gates.

    Every gate is an individually named, non-secret boolean so an unmet gate
    is always recorded — even when the matching capability is disabled and
    therefore considered OK by ``capability_report()``. Adapter registration
    is read from the same code-owned job adapter registry that
    ``inventory_health`` uses; no network or credential value is ever touched.
    """
    from crank.agents.jobs.registry import REGISTRY

    usajobs_configured = bool(
        str(getattr(settings, "USAJOBS_AUTH_KEY", "") or "").strip()
    )
    firecrawl_configured = bool(
        str(getattr(settings, "FIRECRAWL_API_KEY", "") or "").strip()
    )
    adapters = set(REGISTRY.keys())
    return {
        "adapter_registered": len(adapters) > 0,
        "adapter_count": len(adapters),
        "adapters": sorted(adapters),
        "usajobs_adapter_registered": "usajobs" in adapters,
        "firecrawl_adapter_registered": "firecrawl-careers" in adapters,
        "credentials_configured": usajobs_configured or firecrawl_configured,
        "usajobs_credentials_configured": usajobs_configured,
        "firecrawl_credentials_configured": firecrawl_configured,
        "pipeline_enabled": bool(getattr(settings, "JOB_PIPELINE_ENABLED", False)),
        "scheduler_enabled": bool(getattr(settings, "CRAWL_CRON_ENABLED", False)),
    }


def latest_runs() -> list[dict]:
    """Return the latest AgentRun per run type with a sanitized outcome."""
    runs = []
    for run_type, _label in AgentRun.RunType.choices:
        run = (
            AgentRun.objects.filter(run_type=run_type)
            .order_by("-created", "-id")
            .first()
        )
        if run is None:
            continue
        runs.append(
            {
                "run_type": run_type,
                "status": run.status,
                "terminal": run.status in _TERMINAL_RUN_STATUSES,
                "created": run.created.isoformat() if run.created else None,
                "finished_at": (
                    run.finished_at.isoformat() if run.finished_at else None
                ),
                "error_summary": _scrub(run.error_summary),
            }
        )
    return runs


def source_counts() -> dict:
    """Mirror the admin dashboard's configured/approved/enabled aggregates."""
    sources = JobSourceCatalog.objects.all()
    return {
        "configured": sources.count(),
        "approved": sources.filter(
            approval_state=JobSourceCatalog.ApprovalState.APPROVED
        ).count(),
        "enabled": sources.filter(
            approval_state=JobSourceCatalog.ApprovalState.APPROVED, enabled=True
        ).count(),
    }


def baseline_record() -> dict:
    """Assemble the full baseline record from safe helpers only."""
    return {
        "generated_at": timezone.now().isoformat(),
        "env": str(getattr(settings, "ENV", "") or UNKNOWN),
        "source_version": git_sha(),
        "frontend_build_id": frontend_build_id(),
        "migrations": migration_status_summary(),
        "migration_leaves": migration_leaves(),
        "fixtures": fixture_set(),
        "readiness_gates": readiness_gates(),
        "job_search_provider": _safe_job_search_provider(),
        "capabilities": capability_report().to_dict(),
        "inventory": check_inventory_health(),
        "latest_runs": latest_runs(),
        "source_counts": source_counts(),
    }


class Command(BaseCommand):
    help = (
        "Emit a single readiness-baseline record (JSON) describing deployment, "
        "migration, capability, inventory, readiness-gate, run, and source "
        "state. Staff-only evidence; never includes secret values."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            dest="out_path",
            default=None,
            help="Additionally write the JSON record to this file path.",
        )

    def handle(self, *args, **options):
        payload = json.dumps(baseline_record(), indent=2, sort_keys=True)
        self.stdout.write(payload)
        out_path = options.get("out_path")
        if out_path:
            with open(out_path, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
            self.stderr.write(self.style.NOTICE(f"Baseline written to {out_path}"))
