# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Release and deploy-integrity diagnostics for staff-only surfaces.

This module deliberately exposes only safe integrity signals: the backend git
SHA / image identifier, the frontend webpack manifest build identifier, a
bounded migration summary, non-secret feature modes, and aggregate record
counts. It never returns secrets, credentials, prompts, user content, or raw
provider errors.
"""

from __future__ import annotations

import json
import os
import re

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

# Environment variables that carry the backend git SHA / image identifier in
# the order they are preferred. Operators can map a container image label to
# any of these names at deploy time.
_GIT_SHA_ENV_VARS = (
    "GIT_SHA",
    "SOURCE_VERSION",
    "GIT_COMMIT",
    "COMMIT_SHA",
    "REVISION",
    "IMAGE_TAG",
)

# A safe, bounded token: alphanumeric with a small set of separators. This
# rejects values that could be used for template injection or UI confusion.
_GIT_SHA_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")

# Webpack emits ``<entry>.<contenthash>.<ext>``; extract the contenthash.
_CONTENTHASH_PATTERN = re.compile(r"\.([0-9a-f]{8,64})\.(?:js|mjs|css)$")

UNKNOWN = "unknown"


def git_sha() -> str:
    """Return the first set backend git SHA / image identifier, else ``unknown``."""
    for name in _GIT_SHA_ENV_VARS:
        value = os.environ.get(name)
        if value:
            value = value.strip()
            if _GIT_SHA_PATTERN.match(value):
                return value
    return UNKNOWN


def _manifest_path() -> str:
    """Resolve the webpack manifest path from settings, with a safe fallback."""
    manifest_loader = getattr(settings, "MANIFEST_LOADER", {}) or {}
    path = manifest_loader.get("MANIFEST_PATH")
    if path:
        return path
    base_dir = getattr(settings, "BASE_DIR", "") or os.getcwd()
    return os.path.join(str(base_dir), "static", "dist", "manifest.json")


def frontend_build_id() -> str:
    """Return the frontend webpack contenthash from the manifest, else ``unknown``."""
    try:
        with open(_manifest_path(), "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        return UNKNOWN
    main_asset = manifest.get("main.js") or manifest.get("main")
    if not main_asset:
        return UNKNOWN
    match = _CONTENTHASH_PATTERN.search(str(main_asset))
    return match.group(1) if match else str(main_asset)


def migration_status_summary() -> dict:
    """Return a bounded migration summary that never raises on DB failure."""
    try:
        executor = MigrationExecutor(connection)
        applied_count = len(executor.loader.applied_migrations)
        pending_count = len(
            executor.migration_plan(executor.loader.graph.leaf_nodes())
        )
        status = "pending" if pending_count else "clean"
        return {
            "applied_count": applied_count,
            "pending_count": pending_count,
            "status": status,
        }
    except Exception:  # noqa: BLE001 - fail closed on any DB/loader error
        return {
            "applied_count": None,
            "pending_count": None,
            "status": "error",
        }


def config_modes() -> dict:
    """Return non-secret feature modes. Secrets are reduced to booleans or omitted."""
    return {
        "job_search_provider": getattr(settings, "JOB_SEARCH_PROVIDER", ""),
        "llm_configured": bool(getattr(settings, "LLM_PROVIDER", "")),
        "job_pipeline_enabled": bool(
            getattr(settings, "JOB_PIPELINE_ENABLED", False)
        ),
        "crawl_scheduling_enabled": bool(
            getattr(settings, "CRAWL_CRON_ENABLED", False)
        ),
    }


def counts() -> dict:
    """Return aggregate record counts; no user content or provider errors."""
    from crank.models.job import JobListing, JobSourceCatalog

    return {
        "job_source_catalog_count": JobSourceCatalog.objects.count(),
        # ``JobListing.objects`` is the active-only default manager.
        "active_job_listing_count": JobListing.objects.count(),
    }


def diagnostics() -> dict:
    """Assemble the full staff-only release diagnostics payload."""
    return {
        "git_sha": git_sha(),
        "frontend_build_id": frontend_build_id(),
        "migrations": migration_status_summary(),
        "config": config_modes(),
        "counts": counts(),
    }


__all__ = [
    "UNKNOWN",
    "config_modes",
    "counts",
    "diagnostics",
    "frontend_build_id",
    "git_sha",
    "migration_status_summary",
]
