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
from django.core.cache import cache
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
# Permissive on purpose: ``_GIT_SHA_ENV_VARS`` may carry a git SHA-1 (40 hex)
# / SHA-256 (64 hex), a container image tag (e.g. ``v1.2.3``), or a service
# ``SOURCE_VERSION``; we accept any bounded alphanumeric-with-separators
# token rather than a strict SHA-1 regex that would reject valid image tags.
_GIT_SHA_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")

# Env vars, set at deploy time, that pin the release an image was built from.
# ``release_build_status()`` compares the running process' served identifiers
# against these so stale webpack assets can never be served silently.
_EXPECTED_BACKEND_ENV_VARS = ("RELEASE_BACKEND_SHA", "EXPECTED_BACKEND_SHA")
_EXPECTED_FRONTEND_ENV_VARS = ("RELEASE_FRONTEND_BUILD", "EXPECTED_FRONTEND_BUILD")

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
    # A bare ``main.js`` (no contenthash, e.g. a dev build) is not a build
    # identifier. Return ``unknown`` rather than a misleading raw asset name.
    return match.group(1) if match else UNKNOWN


def migration_status_summary() -> dict:
    """Return a bounded migration summary that never raises on DB failure.

    Pending migrations are detected by comparing each graph leaf against the
    applied set (constant-time dict lookups) instead of computing a full
    ``migration_plan`` walk from every leaf. This stays bounded on projects
    with many apps/migrations and never holds a long-lived DB lock: the only
    DB work is the single applied-migrations query the loader already makes.
    """
    try:
        executor = MigrationExecutor(connection)
        applied = executor.loader.applied_migrations
        pending_count = sum(
            1 for leaf in executor.loader.graph.leaf_nodes() if leaf not in applied
        )
        status = "pending" if pending_count else "clean"
        return {
            "applied_count": len(applied),
            "pending_count": pending_count,
            "status": status,
        }
    except Exception:  # noqa: BLE001 - fail closed on any DB/loader error
        return {
            "applied_count": None,
            "pending_count": None,
            "status": "error",
        }


# Provider names are non-secret, but bound them to a safe token so a
# misconfigured ``JOB_SEARCH_PROVIDER`` (e.g. a URL with embedded creds) is
# never echoed verbatim onto the diagnostics page.
_PROVIDER_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")


def _safe_job_search_provider() -> str:
    value = str(getattr(settings, "JOB_SEARCH_PROVIDER", "") or "").strip()
    return value if _PROVIDER_PATTERN.match(value) else UNKNOWN


def config_modes() -> dict:
    """Return non-secret feature modes. Secrets are reduced to booleans or omitted."""
    return {
        "job_search_provider": _safe_job_search_provider(),
        "llm_configured": bool(getattr(settings, "LLM_PROVIDER", "")),
        "job_pipeline_enabled": bool(
            getattr(settings, "JOB_PIPELINE_ENABLED", False)
        ),
        "crawl_scheduling_enabled": bool(
            getattr(settings, "CRAWL_CRON_ENABLED", False)
        ),
    }


# Full-table COUNT(*) is cached briefly so the staff diagnostics page never
# pays a scan on every load. These are advisory deployment signals, not live
# metrics, so a short TTL is acceptable.
_COUNTS_CACHE_KEY = "release-diagnostics:counts"
_COUNTS_CACHE_TTL_SECONDS = 60


def _compute_counts() -> dict:
    from crank.models.job import JobListing, JobSourceCatalog

    return {
        "job_source_catalog_count": JobSourceCatalog.objects.count(),
        # Filter explicitly on ACTIVE rather than relying on ``objects`` being
        # the active-only default manager, so the count is correct even if the
        # default manager changes later.
        "active_job_listing_count": JobListing.objects.filter(
            status=JobListing.Status.ACTIVE
        ).count(),
    }


def counts() -> dict:
    """Return aggregate record counts; no user content or provider errors."""
    return cache.get_or_set(
        _COUNTS_CACHE_KEY,
        _compute_counts,
        _COUNTS_CACHE_TTL_SECONDS,
    )


def _expected_build(env_names: tuple[str, ...]) -> str:
    """Return the first deploy-pinned release identifier, else ``""`` if unpinned."""
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return ""


def release_build_status() -> dict:
    """Alert on release drift between the deploy-pinned and served builds.

    At deploy time operators pin the expected backend image/commit and webpack
    contenthash via ``RELEASE_BACKEND_SHA`` and ``RELEASE_FRONTEND_BUILD``.
    When the running process reads a different git SHA or serves a manifest
    that differs from what was pinned, this reports a mismatch so stale webpack
    assets can never be served silently.

    Returns:
        ``{"status": "ok", "mismatched": []}`` when every pinned identifier
        matches the served value; ``{"status": "mismatch", "mismatched":
        ["backend"|"frontend"]}`` when a pinned identifier drifts; or
        ``{"status": "unverifiable", "mismatched": []}`` when no release is
        pinned (never silently green).
    """
    expected_backend = _expected_build(_EXPECTED_BACKEND_ENV_VARS)
    expected_frontend = _expected_build(_EXPECTED_FRONTEND_ENV_VARS)
    if not expected_backend and not expected_frontend:
        return {"status": "unverifiable", "mismatched": []}

    mismatched = []
    if expected_backend and git_sha() != expected_backend:
        mismatched.append("backend")
    if expected_frontend and frontend_build_id() != expected_frontend:
        mismatched.append("frontend")
    if mismatched:
        return {"status": "mismatch", "mismatched": mismatched}
    return {"status": "ok", "mismatched": []}


def diagnostics() -> dict:
    """Assemble the full staff-only release diagnostics payload."""
    return {
        "git_sha": git_sha(),
        "frontend_build_id": frontend_build_id(),
        "build": release_build_status(),
        "migrations": migration_status_summary(),
        "config": config_modes(),
        "counts": counts(),
    }


__all__ = [
    "UNKNOWN",
    "_EXPECTED_BACKEND_ENV_VARS",
    "_EXPECTED_FRONTEND_ENV_VARS",
    "config_modes",
    "counts",
    "diagnostics",
    "frontend_build_id",
    "git_sha",
    "migration_status_summary",
    "release_build_status",
]
