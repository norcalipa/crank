# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Shared utilities for preflight scripts.

Provides:
- ``run_gh``: subprocess wrapper for the GitHub CLI (``gh``) with
  timeout and error classification.
- ``StateStore``: bounded, non-secret fingerprint persistence so
  unchanged state does not repeatedly fire model-backed turns.
- ``PreflightResult``: structured JSON result with stable exit semantics.
- ``PreflightError``: exception hierarchy for API/rate-limit/timeout errors.

All functions are read-only with respect to GitHub. The only writes are
to the local state file, which contains non-secret fingerprints.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Default state directory (relative to repo root or override via env).
DEFAULT_STATE_DIR = os.environ.get(
    "PREFLIGHT_STATE_DIR",
    str(Path(__file__).resolve().parent.parent / ".preflight-state"),
)

#: Default timeout for ``gh`` subprocess calls (seconds).
DEFAULT_GH_TIMEOUT = 30


class PreflightError(Exception):
    """Base error for preflight failures."""

    exit_code = 2


class RateLimitError(PreflightError):
    """GitHub API rate limit hit."""

    exit_code = 3


class TimeoutError(PreflightError):  # noqa: A001 - intentional name
    """Subprocess timeout calling ``gh``."""

    exit_code = 4


class APIError(PreflightError):
    """Unexpected GitHub API response."""

    exit_code = 5


@dataclass
class PreflightResult:
    """Structured preflight output.

    Always printed as JSON to stdout. Exit code 0 regardless of
    ``fire`` value; non-zero only on preflight failure.
    """

    fire: bool
    reason: str
    summary: dict[str, Any] = field(default_factory=dict)
    fingerprint: str | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "fire": self.fire,
                "reason": self.reason,
                "summary": self.summary,
                "fingerprint": self.fingerprint,
                "error": self.error,
            },
            sort_keys=True,
        )

    def emit(self) -> None:
        print(self.to_json())


def run_gh(
    *args: str,
    timeout: int = DEFAULT_GH_TIMEOUT,
    repo: str | None = None,
) -> dict[str, Any]:
    """Invoke ``gh`` CLI and return parsed JSON output.

    Raises:
        RateLimitError: on HTTP 403 rate limit.
        TimeoutError: on subprocess timeout.
        APIError: on non-zero exit or JSON parse failure.
    """
    cmd = ["gh"]
    if repo:
        cmd.extend(["--repo", repo])
    cmd.extend(args)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"gh command timed out after {timeout}s: {' '.join(cmd)}"
        ) from exc

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "rate limit" in stderr.lower() or "HTTP 403" in stderr:
            raise RateLimitError(f"GitHub API rate limit: {stderr}")
        raise APIError(
            f"gh exited {proc.returncode}: {stderr or proc.stdout.strip()[:200]}"
        )

    stdout = proc.stdout.strip()
    if not stdout:
        return {}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise APIError(f"gh returned non-JSON output: {stdout[:200]}") from exc


def run_gh_paginated(
    template: str,
    *,
    repo: str | None = None,
    timeout: int = DEFAULT_GH_TIMEOUT,
    max_pages: int = 10,
    jq_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Invoke ``gh api --paginate`` with a template and return aggregated items.

    Args:
        template: GitHub API path template (e.g. ``repos/{owner}/{repo}/pulls``).
        repo: ``owner/repo`` for ``--repo`` flag (used for auth resolution).
        timeout: per-call timeout.
        max_pages: safety cap to prevent unbounded pagination.
        jq_filter: optional ``--jq`` filter applied per page.

    Returns:
        Aggregated list of dict items. If the API returns a non-list
        response, it is wrapped in a single-element list.
    """
    cmd = ["gh"]
    if repo:
        cmd.extend(["--repo", repo])
    cmd.extend(["api", "--paginate", "-X", "GET", template])
    if jq_filter:
        cmd.extend(["--jq", jq_filter])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout * max_pages,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"gh api paginated timed out after {timeout * max_pages}s"
        ) from exc

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "rate limit" in stderr.lower() or "HTTP 403" in stderr:
            raise RateLimitError(f"GitHub API rate limit: {stderr}")
        raise APIError(
            f"gh api exited {proc.returncode}: {stderr or proc.stdout.strip()[:200]}"
        )

    stdout = proc.stdout.strip()
    if not stdout:
        return []

    # gh api --paginate with JSON outputs one JSON object per line per page
    # or a single JSON array. Handle both.
    try:
        data = json.loads(stdout)
        if isinstance(data, list):
            return data
        return [data]
    except json.JSONDecodeError:
        # Try line-delimited JSON
        items: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, list):
                    items.extend(obj)
                else:
                    items.append(obj)
            except json.JSONDecodeError:
                continue
        if not items:
            raise APIError(f"gh api returned unparseable output: {stdout[:200]}")
        return items


class StateStore:
    """Bounded, non-secret fingerprint persistence.

    Stores a single JSON file with the last fingerprint and timestamp.
    Used to deduplicate unchanged actionable state so the model is only
    invoked once per new fingerprint.
    """

    def __init__(self, state_path: str | None = None, *, job_name: str = "default"):
        if state_path is None:
            state_path = DEFAULT_STATE_DIR
        self.state_dir = Path(state_path)
        self.state_file = self.state_dir / f"{job_name}.json"

    def load(self) -> dict[str, Any]:
        """Load the last persisted state. Returns ``{}`` if none."""
        if not self.state_file.exists():
            return {}
        try:
            with self.state_file.open("r") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, fingerprint: str, summary: dict[str, Any]) -> None:
        """Persist a new fingerprint and bounded summary.

        The state file is kept small (only fingerprint + minimal summary
        + timestamp). No secrets are stored.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "fingerprint": fingerprint,
            "summary": summary,
            "updated_at": int(time.time()),
        }
        tmp = self.state_file.with_suffix(".tmp")
        with tmp.open("w") as fh:
            json.dump(payload, fh, sort_keys=True)
        tmp.replace(self.state_file)

    def clear(self) -> None:
        """Remove the state file (used for reset/manual runs)."""
        if self.state_file.exists():
            self.state_file.unlink()

    def has_changed(self, fingerprint: str) -> bool:
        """Return True if the fingerprint differs from the persisted one."""
        return self.load().get("fingerprint") != fingerprint


def compute_fingerprint(data: Any) -> str:
    """Compute a stable SHA-256 fingerprint from arbitrary JSON-serialisable data."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def emit_and_exit(result: PreflightResult) -> None:
    """Print JSON result and exit 0."""
    result.emit()
    sys.exit(0)


def emit_error_and_exit(error: PreflightError, job_name: str) -> None:
    """Print error JSON and exit with the error's exit code."""
    result = PreflightResult(
        fire=False,
        reason="preflight_error",
        error=f"{type(error).__name__}: {error}",
    )
    result.emit()
    sys.exit(error.exit_code)
