# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""PR convergence preflight for OpenClaw ``crank-pr-ci-conflict-convergence`` job.

Inspects only the target repo via ``gh`` CLI. Computes an actionable
fingerprint from:

- Open factory/managed PR numbers and head SHAs
- Mergeability/conflict state
- Required-check state at the current head
- Unresolved actionable review/comment gates
- Merge/readiness labels relevant to the factory contract

Returns ``{fire: false}`` when no managed PRs are open or all current
heads require no convergence action. Returns ``{fire: true}`` once for a
new actionable fingerprint, including a minimal state summary needed by
the worker.

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
import sys
from typing import Any

# Allow running both as a script and as an importable module for tests.
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
        run_gh_paginated,
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
        run_gh_paginated,
    )

#: Labels that mark a PR as managed by the software-factory contract.
MANAGED_LABELS = frozenset({
    "sf-managed",
    "factory",
    "auto-merge",
})

#: Labels that signal merge readiness or gating.
READINESS_LABELS = frozenset({
    "ready-to-merge",
    "merge-when-passing",
    "do-not-merge",
    "blocked",
})

#: Default target repo (owner/repo). Override with ``GITHUB_REPOSITORY`` env.
DEFAULT_REPO = os.environ.get("GITHUB_REPOSITORY", "")

JOB_NAME = "pr-convergence"


def _is_managed_pr(pr: dict[str, Any]) -> bool:
    """Return True if a PR has any managed/factory label."""
    labels = {lbl.get("name", "") for lbl in pr.get("labels", [])}
    return bool(labels & MANAGED_LABELS)


def _extract_pr_state(pr: dict[str, Any]) -> dict[str, Any]:
    """Extract the actionable state fields from a PR dict.

    Returns a minimal dict with: number, head_sha, mergeable, mergeable_state,
    labels, review_state, check_summary.
    """
    number = pr.get("number")
    head = pr.get("head", {}) or {}
    head_sha = head.get("sha", "")

    # mergeable and mergeable_state from the PR object
    mergeable = pr.get("mergeable")
    mergeable_state = pr.get("mergeable_state", "")

    # Labels
    label_names = sorted(
        lbl.get("name", "")
        for lbl in pr.get("labels", [])
        if lbl.get("name")
    )

    # Review state — summary of requested/approved
    reviews = pr.get("reviews", []) or []
    review_state = _summarize_reviews(reviews)

    # Check summary from statusCheckRollup (GraphQL) or separate API call
    checks = pr.get("statusCheckRollup", []) or []
    check_summary = _summarize_checks(checks)

    return {
        "number": number,
        "head_sha": head_sha,
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "labels": label_names,
        "review_state": review_state,
        "check_summary": check_summary,
    }


def _summarize_reviews(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize review state into actionable categories."""
    if not reviews:
        return {"approved": 0, "changes_requested": 0, "pending": 0}

    approved = 0
    changes_requested = 0
    pending = 0

    # Track latest review per author
    latest_by_author: dict[str, str] = {}
    for rev in reviews:
        author = rev.get("author", {}).get("login", "") or rev.get("user", {}).get("login", "")
        state = rev.get("state", "").upper()
        if not author:
            continue
        latest_by_author[author] = state

    for state in latest_by_author.values():
        if state == "APPROVED":
            approved += 1
        elif state == "CHANGES_REQUESTED":
            changes_requested += 1
        else:
            pending += 1

    return {
        "approved": approved,
        "changes_requested": changes_requested,
        "pending": pending,
    }


def _summarize_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize check statuses into counts."""
    if not checks:
        return {"total": 0, "passing": 0, "failing": 0, "pending": 0}

    passing = 0
    failing = 0
    pending = 0

    for check in checks:
        # GraphQL statusCheckRollup uses 'conclusion' or 'status'
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()

        if conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            failing += 1
        elif conclusion == "SUCCESS":
            passing += 1
        elif status in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING"):
            pending += 1
        elif status == "COMPLETED":
            # Completed with no conclusion counts as passing
            passing += 1
        else:
            pending += 1

    return {
        "total": len(checks),
        "passing": passing,
        "failing": failing,
        "pending": pending,
    }


def _is_actionable(pr_state: dict[str, Any]) -> bool:
    """Determine if a PR state requires convergence work.

    A PR is actionable when any of:
    - mergeable is False (conflict)
    - mergeable_state is 'blocked' or 'dirty'
    - check_summary has failing checks
    - review_state has changes_requested
    - has a readiness label indicating blocked
    """
    if pr_state.get("mergeable") is False:
        return True
    ms = pr_state.get("mergeable_state", "")
    if ms in ("blocked", "dirty", "unstable"):
        return True
    if pr_state.get("check_summary", {}).get("failing", 0) > 0:
        return True
    if pr_state.get("review_state", {}).get("changes_requested", 0) > 0:
        return True
    labels = set(pr_state.get("labels", []))
    if "blocked" in labels or "do-not-merge" in labels:
        return True
    return False


def compute_actionable_fingerprint(pr_states: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Compute fingerprint from actionable PR states only.

    Returns (fingerprint, actionable_prs).
    """
    actionable = [ps for ps in pr_states if _is_actionable(ps)]
    # Sort by PR number for stability
    actionable.sort(key=lambda ps: ps.get("number", 0))
    fingerprint_data = [
        {
            "number": ps.get("number"),
            "head_sha": ps.get("head_sha"),
            "mergeable": ps.get("mergeable"),
            "mergeable_state": ps.get("mergeable_state"),
            "labels": ps.get("labels"),
            "review_state": ps.get("review_state"),
            "check_summary": ps.get("check_summary"),
        }
        for ps in actionable
    ]
    fp = compute_fingerprint(fingerprint_data) if fingerprint_data else ""
    return fp, actionable


def fetch_prs(repo: str) -> list[dict[str, Any]]:
    """Fetch open managed PRs from the target repo using ``gh`` CLI.

    Uses GraphQL via ``gh api`` to get PRs with labels, reviews, and
    check rollups in a single paginated call.
    """
    # Use REST API for open PRs, then enrich per-PR as needed
    # gh api repos/{owner}/{repo}/pulls --field state=open
    prs = run_gh_paginated(
        f"repos/{repo}/pulls",
        repo=repo,
    )

    # Filter to open PRs (should already be open from state=open, but double-check)
    open_prs = [pr for pr in prs if pr.get("state") == "open"]

    # Filter to managed PRs only
    managed = [pr for pr in open_prs if _is_managed_pr(pr)]
    return managed


def fetch_pr_details(repo: str, pr_number: int) -> dict[str, Any]:
    """Fetch detailed PR info including mergeability and reviews.

    Uses individual API calls to get:
    - PR details (mergeability, mergeable_state)
    - Reviews
    - Combined status checks
    """
    # Get PR details
    pr_detail = run_gh(
        "api",
        f"repos/{repo}/pulls/{pr_number}",
        repo=repo,
    )

    # Get reviews
    reviews = run_gh_paginated(
        f"repos/{repo}/pulls/{pr_number}/reviews",
        repo=repo,
    )
    pr_detail["reviews"] = reviews

    # Get combined status checks for the head SHA
    head_sha = pr_detail.get("head", {}).get("sha", "")
    if head_sha:
        try:
            status = run_gh(
                "api",
                f"repos/{repo}/commits/{head_sha}/check-runs",
                repo=repo,
            )
            check_runs = status.get("check_runs", []) if isinstance(status, dict) else []
            pr_detail["statusCheckRollup"] = check_runs
        except APIError:
            # Check runs may not be available for all repos; continue without
            pr_detail["statusCheckRollup"] = []

    return pr_detail


def run_preflight(
    repo: str | None = None,
    state_dir: str | None = None,
    *,
    prs_override: list[dict[str, Any]] | None = None,
    details_override: dict[int, dict[str, Any]] | None = None,
) -> PreflightResult:
    """Run the PR convergence preflight.

    Args:
        repo: ``owner/repo`` target. Defaults to ``GITHUB_REPOSITORY`` env.
        state_dir: Override state directory.
        prs_override: Inject PR list directly (for testing).
        details_override: Inject PR details dict keyed by number (for testing).

    Returns:
        PreflightResult with fire/no-fire and summary.
    """
    if not repo and prs_override is None:
        return PreflightResult(
            fire=False,
            reason="no_repo configured",
            error="GITHUB_REPOSITORY not set and no repo argument provided",
        )

    store = StateStore(state_dir, job_name=JOB_NAME)

    # Fetch managed PRs (or use override for testing)
    if prs_override is not None:
        managed_prs = [pr for pr in prs_override if _is_managed_pr(pr)]
    else:
        managed_prs = fetch_prs(repo)

    if not managed_prs:
        # No managed PRs → no work
        store.save("", {})
        return PreflightResult(
            fire=False,
            reason="no_managed_prs",
            fingerprint="",
        )

    # Fetch detailed state for each managed PR (or use override)
    pr_states: list[dict[str, Any]] = []
    for pr in managed_prs:
        number = pr.get("number")
        if details_override is not None and number in details_override:
            detailed = details_override[number]
        elif prs_override is not None:
            # When PRs are overridden, use the PR dict as-is if it has
            # the needed fields, otherwise extract from what we have
            detailed = pr
        else:
            detailed = fetch_pr_details(repo, number)

        pr_states.append(_extract_pr_state(detailed))

    # Compute actionable fingerprint
    fingerprint, actionable_prs = compute_actionable_fingerprint(pr_states)

    if not actionable_prs:
        # All PRs are clean → no work. Save empty fingerprint so a future
        # transition to actionable will fire.
        store.save("", {})
        return PreflightResult(
            fire=False,
            reason="no_actionable_prs",
            fingerprint="",
        )

    # Check if fingerprint changed
    if not store.has_changed(fingerprint):
        # Same actionable state as last run → don't re-fire
        return PreflightResult(
            fire=False,
            reason="unchanged_actionable_state",
            fingerprint=fingerprint,
        )

    # New actionable state → fire
    store.save(fingerprint, {"actionable_prs": len(actionable_prs)})
    summary = {
        "actionable_pr_count": len(actionable_prs),
        "prs": [
            {
                "number": ps.get("number"),
                "head_sha": ps.get("head_sha"),
                "mergeable": ps.get("mergeable"),
                "mergeable_state": ps.get("mergeable_state"),
                "failing_checks": ps.get("check_summary", {}).get("failing", 0),
                "changes_requested": ps.get("review_state", {}).get("changes_requested", 0),
                "labels": ps.get("labels"),
            }
            for ps in actionable_prs
        ],
    }
    return PreflightResult(
        fire=True,
        reason="new_actionable_fingerprint",
        summary=summary,
        fingerprint=fingerprint,
    )


def main() -> None:
    """Entry point for the preflight script."""
    try:
        result = run_preflight(repo=DEFAULT_REPO)
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
