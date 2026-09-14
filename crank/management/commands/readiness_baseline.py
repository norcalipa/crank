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
from django.utils import timezone

from crank.capability import capability_report
from crank.models.agent_run import AgentRun
from crank.models.job import JobSourceCatalog
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

#: Minimum length for a settings value to be treated as a scrub-able secret.
#: Short values would mangle output text without adding protection.
_SECRET_MIN_LENGTH = 6

#: Error summaries are already bounded/sanitized at write time; this is
#: defense-in-depth for the baseline record itself.
_ERROR_SUMMARY_MAX_CHARS = 500

#: AgentRun statuses that represent a completed (terminal) outcome.
_TERMINAL_RUN_STATUSES = frozenset(
    {AgentRun.Status.SUCCEEDED, AgentRun.Status.FAILED, AgentRun.Status.SKIPPED}
)


def _configured_secrets() -> list[str]:
    """Return non-empty configured secret values long enough to scrub."""
    values = []
    for name in _SECRET_SETTING_NAMES:
        value = str(getattr(settings, name, "") or "")
        if len(value) >= _SECRET_MIN_LENGTH:
            values.append(value)
    return values


def _scrub(text: object) -> str:
    """Redact configured secret values from free text and bound its length."""
    text = str(text or "")
    for secret in _configured_secrets():
        if secret in text:
            text = text.replace(secret, "[redacted]")
    return text[:_ERROR_SUMMARY_MAX_CHARS]


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
        "job_search_provider": _safe_job_search_provider(),
        "capabilities": capability_report().to_dict(),
        "inventory": check_inventory_health(),
        "latest_runs": latest_runs(),
        "source_counts": source_counts(),
    }


class Command(BaseCommand):
    help = (
        "Emit a single readiness-baseline record (JSON) describing deployment, "
        "migration, capability, inventory, run, and source state. Staff-only "
        "evidence; never includes secret values."
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
