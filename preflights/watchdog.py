# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Watchdog preflight for OpenClaw ``sf-watchdog`` job.

Runs deterministic audits (including the existing task audit) and returns
``{fire: false}`` when there are no ``warn``/``error`` findings and no
changed failure state. Uses a command-only payload for reports/remediation
that do not require reasoning; invokes an agent only for findings that
genuinely require investigation.

Persist a bounded, non-secret fingerprint/state so unchanged findings do
not repeatedly fire the model. A resolved→actionable or changed finding
state must fire again.

Exit codes:
    0 — success (fire or no-fire)
    3 — rate limit
    4 — timeout
    5 — API error
    2 — unexpected error
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from preflights.common import (  # type: ignore[no-redef]
        APIError,
        PreflightError,
        PreflightResult,
        RateLimitError,
        StateStore,
        TimeoutError,
        compute_fingerprint,
        emit_and_exit,
        emit_error_and_exit,
        run_gh,
    )
else:
    from .common import (
        APIError,
        PreflightError,
        PreflightResult,
        RateLimitError,
        StateStore,
        TimeoutError,
        compute_fingerprint,
        emit_and_exit,
        emit_error_and_exit,
        run_gh,
    )

JOB_NAME = "watchdog"

#: Default repo (owner/repo). Override with ``GITHUB_REPOSITORY`` env.
DEFAULT_REPO = os.environ.get("GITHUB_REPOSITORY", "")

#: Severities that warrant model-backed investigation.
ACTIONABLE_SEVERITIES = frozenset({"warn", "error"})


def _run_subprocess(
    cmd: list[str],
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"


def audit_github_actions(repo: str) -> list[dict[str, Any]]:
    """Audit GitHub Actions workflow runs for failures.

    Returns list of findings with severity, source, and details.
    """
    findings: list[dict[str, Any]] = []
    try:
        result = run_gh(
            "api",
            f"repos/{repo}/actions/runs",
            repo=repo,
        )
    except (APIError, RateLimitError, TimeoutError):
        # If we can't reach the API, report it as a warning
        findings.append({
            "severity": "warn",
            "source": "github_actions",
            "message": "Unable to fetch GitHub Actions runs",
        })
        return findings

    workflow_runs = result.get("workflow_runs", []) if isinstance(result, dict) else []
    # Only look at the most recent 20 runs
    for run in workflow_runs[:20]:
        status = run.get("status", "")
        conclusion = run.get("conclusion", "")
        if status == "completed" and conclusion in ("failure", "cancelled"):
            findings.append({
                "severity": "error",
                "source": "github_actions",
                "message": f"Workflow run {run.get('id')} concluded as {conclusion}",
                "details": {
                    "run_id": run.get("id"),
                    "workflow_name": run.get("name"),
                    "branch": run.get("head_branch"),
                    "conclusion": conclusion,
                    "html_url": run.get("html_url"),
                },
            })
    return findings


def audit_django_checks() -> list[dict[str, Any]]:
    """Run Django system checks and report any warnings/errors.

    This is a deterministic, read-only check that runs ``manage.py check``
    and parses the output.
    """
    findings: list[dict[str, Any]] = []
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    rc, stdout, stderr = _run_subprocess(
        [sys.executable, "manage.py", "check", "--verbosity", "2"],
        timeout=30,
    )

    if rc != 0:
        findings.append({
            "severity": "error",
            "source": "django_check",
            "message": f"Django system check exited {rc}",
            "details": {"stderr": stderr.strip()[:500]},
        })
    elif "warning" in stdout.lower():
        # Django checks output warnings to stdout
        findings.append({
            "severity": "warn",
            "source": "django_check",
            "message": "Django system check produced warnings",
            "details": {"output": stdout.strip()[:500]},
        })

    return findings


def audit_open_prs_aged(repo: str) -> list[dict[str, Any]]:
    """Check for PRs that have been open and stale for too long.

    Flags PRs older than 7 days with no activity in 3 days as warnings.
    """
    findings: list[dict[str, Any]] = []
    try:
        result = run_gh(
            "api",
            f"repos/{repo}/pulls",
            repo=repo,
        )
    except (APIError, RateLimitError, TimeoutError):
        findings.append({
            "severity": "warn",
            "source": "aged_prs",
            "message": "Unable to fetch PRs for aged-PR audit",
        })
        return findings

    prs = result if isinstance(result, list) else []
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    for pr in prs:
        if pr.get("state") != "open":
            continue
        updated_at_str = pr.get("updated_at", "")
        if not updated_at_str:
            continue
        try:
            updated_at = datetime.datetime.fromisoformat(
                updated_at_str.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            continue
        delta = now - updated_at
        if delta.days > 3:
            findings.append({
                "severity": "warn",
                "source": "aged_prs",
                "message": f"PR #{pr.get('number')} stale ({delta.days} days since update)",
                "details": {
                    "pr_number": pr.get("number"),
                    "title": pr.get("title", "")[:100],
                    "days_stale": delta.days,
                },
            })
    return findings


def collect_findings(
    repo: str | None = None,
    *,
    github_actions_override: list[dict[str, Any]] | None = None,
    django_checks_override: list[dict[str, Any]] | None = None,
    aged_prs_override: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Collect all audit findings.

    Args:
        repo: ``owner/repo`` target.
        github_actions_override: Inject findings directly (for testing).
        django_checks_override: Inject findings directly (for testing).
        aged_prs_override: Inject findings directly (for testing).

    Returns:
        List of finding dicts with ``severity``, ``source``, ``message``.
    """
    repo = repo or DEFAULT_REPO
    findings: list[dict[str, Any]] = []

    if github_actions_override is not None:
        findings.extend(github_actions_override)
    elif repo:
        findings.extend(audit_github_actions(repo))

    if django_checks_override is not None:
        findings.extend(django_checks_override)
    else:
        findings.extend(audit_django_checks())

    if aged_prs_override is not None:
        findings.extend(aged_prs_override)
    elif repo:
        findings.extend(audit_open_prs_aged(repo))

    return findings


def _filter_actionable(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only findings with actionable severity (warn/error)."""
    return [f for f in findings if f.get("severity") in ACTIONABLE_SEVERITIES]


def run_preflight(
    repo: str | None = None,
    state_dir: str | None = None,
    *,
    findings_override: list[dict[str, Any]] | None = None,
    **audit_overrides: Any,
) -> PreflightResult:
    """Run the watchdog preflight.

    Args:
        repo: ``owner/repo`` target. Defaults to ``GITHUB_REPOSITORY`` env.
        state_dir: Override state directory.
        findings_override: Inject findings directly (for testing).
        **audit_overrides: Pass through to ``collect_findings``.

    Returns:
        PreflightResult with fire/no-fire and findings summary.
    """
    repo = repo or DEFAULT_REPO
    store = StateStore(state_dir, job_name=JOB_NAME)

    if findings_override is not None:
        findings = findings_override
    else:
        findings = collect_findings(repo, **audit_overrides)

    actionable = _filter_actionable(findings)

    if not actionable:
        # No actionable findings → no work. Save empty fingerprint.
        store.save("", {})
        return PreflightResult(
            fire=False,
            reason="no_actionable_findings",
            fingerprint="",
        )

    # Compute fingerprint from actionable findings
    # Sort for stability
    actionable_sorted = sorted(
        actionable,
        key=lambda f: (f.get("source", ""), f.get("message", ""), str(f.get("details", ""))),
    )
    fingerprint = compute_fingerprint(actionable_sorted)

    # Check if fingerprint changed
    if not store.has_changed(fingerprint):
        # Same findings as last run → don't re-fire
        return PreflightResult(
            fire=False,
            reason="unchanged_findings",
            fingerprint=fingerprint,
        )

    # New actionable findings → fire
    store.save(fingerprint, {"finding_count": len(actionable)})
    summary = {
        "finding_count": len(actionable),
        "findings": [
            {
                "severity": f.get("severity"),
                "source": f.get("source"),
                "message": f.get("message"),
            }
            for f in actionable_sorted
        ],
    }
    return PreflightResult(
        fire=True,
        reason="new_findings",
        summary=summary,
        fingerprint=fingerprint,
    )


def main() -> None:
    """Entry point for the watchdog preflight script."""
    try:
        result = run_preflight()
        emit_and_exit(result)
    except RateLimitError as exc:
        emit_error_and_exit(exc, JOB_NAME)
    except TimeoutError as exc:
        emit_error_and_exit(exc, JOB_NAME)
    except APIError as exc:
        emit_error_and_exit(exc, JOB_NAME)
    except PreflightError as exc:
        emit_error_and_exit(exc, JOB_NAME)
    except Exception as exc:
        result = PreflightResult(
            fire=False,
            reason="unexpected_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        result.emit()
        sys.exit(2)


if __name__ == "__main__":
    main()
