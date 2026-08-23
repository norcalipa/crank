# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for preflights.watchdog: no-work, actionable, unchanged, recovery, API failure, rate limit."""
import json
import os
import tempfile
from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase

from preflights.common import (
    APIError,
    PreflightResult,
    RateLimitError,
    StateStore,
    TimeoutError as PreflightTimeoutError,
)
from preflights.watchdog import (
    _filter_actionable,
    collect_findings,
    run_preflight,
    audit_github_actions,
    audit_django_checks,
    audit_open_prs_aged,
    ACTIONABLE_SEVERITIES,
    JOB_NAME,
)


class FilterActionableTests(SimpleTestCase):
    def test_filters_out_info(self):
        findings = [
            {"severity": "error", "source": "s1", "message": "m1"},
            {"severity": "warn", "source": "s2", "message": "m2"},
            {"severity": "info", "source": "s3", "message": "m3"},
        ]
        actionable = _filter_actionable(findings)
        self.assertEqual(len(actionable), 2)

    def test_empty_findings(self):
        self.assertEqual(_filter_actionable([]), [])

    def test_all_actionable(self):
        findings = [
            {"severity": "error", "source": "s1"},
            {"severity": "warn", "source": "s2"},
        ]
        self.assertEqual(len(_filter_actionable(findings)), 2)

    def test_no_actionable(self):
        findings = [
            {"severity": "info", "source": "s1"},
            {"severity": "debug", "source": "s2"},
        ]
        self.assertEqual(_filter_actionable(findings), [])


class CollectFindingsTests(SimpleTestCase):
    def test_override_github_actions(self):
        findings = collect_findings(
            "owner/repo",
            github_actions_override=[{"severity": "error", "source": "test"}],
            django_checks_override=[],
            aged_prs_override=[],
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["source"], "test")

    def test_override_all(self):
        findings = collect_findings(
            "owner/repo",
            github_actions_override=[{"severity": "warn", "source": "gh"}],
            django_checks_override=[{"severity": "error", "source": "django"}],
            aged_prs_override=[{"severity": "warn", "source": "aged"}],
        )
        self.assertEqual(len(findings), 3)

    def test_empty_overrides(self):
        findings = collect_findings(
            "owner/repo",
            github_actions_override=[],
            django_checks_override=[],
            aged_prs_override=[],
        )
        self.assertEqual(findings, [])


class AuditGithubActionsTests(SimpleTestCase):
    @patch("preflights.watchdog.run_gh")
    def test_failing_workflow_run(self, mock_gh):
        mock_gh.return_value = {
            "workflow_runs": [
                {"id": 1, "status": "completed", "conclusion": "failure", "name": "CI", "head_branch": "main", "html_url": "http://example.com"},
                {"id": 2, "status": "completed", "conclusion": "success", "name": "CI", "head_branch": "main", "html_url": "http://example.com"},
            ]
        }
        findings = audit_github_actions("owner/repo")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "error")
        self.assertEqual(findings[0]["details"]["run_id"], 1)

    @patch("preflights.watchdog.run_gh")
    def test_cancelled_workflow_run(self, mock_gh):
        mock_gh.return_value = {
            "workflow_runs": [
                {"id": 3, "status": "completed", "conclusion": "cancelled", "name": "CI", "head_branch": "main", "html_url": "http://example.com"},
            ]
        }
        findings = audit_github_actions("owner/repo")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "error")

    @patch("preflights.watchdog.run_gh")
    def test_no_failures(self, mock_gh):
        mock_gh.return_value = {
            "workflow_runs": [
                {"id": 4, "status": "completed", "conclusion": "success", "name": "CI", "head_branch": "main", "html_url": "http://example.com"},
            ]
        }
        findings = audit_github_actions("owner/repo")
        self.assertEqual(findings, [])

    @patch("preflights.watchdog.run_gh")
    def test_api_failure_returns_warning(self, mock_gh):
        mock_gh.side_effect = APIError("server error")
        findings = audit_github_actions("owner/repo")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warn")
        self.assertEqual(findings[0]["source"], "github_actions")

    @patch("preflights.watchdog.run_gh")
    def test_rate_limit_returns_warning(self, mock_gh):
        mock_gh.side_effect = RateLimitError("rate limited")
        findings = audit_github_actions("owner/repo")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warn")

    @patch("preflights.watchdog.run_gh")
    def test_only_recent_20_runs_checked(self, mock_gh):
        runs = [{"id": i, "status": "completed", "conclusion": "failure", "name": f"CI-{i}", "head_branch": "main", "html_url": ""} for i in range(30)]
        mock_gh.return_value = {"workflow_runs": runs}
        findings = audit_github_actions("owner/repo")
        self.assertEqual(len(findings), 20)


class RunPreflightWatchdogTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_findings_returns_no_fire(self):
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            findings_override=[],
        )
        self.assertFalse(result.fire)
        self.assertEqual(result.reason, "no_actionable_findings")

    def test_actionable_findings_fire(self):
        findings = [
            {"severity": "error", "source": "github_actions", "message": "CI failed"},
        ]
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            findings_override=findings,
        )
        self.assertTrue(result.fire)
        self.assertEqual(result.reason, "new_findings")
        self.assertEqual(result.summary["finding_count"], 1)
        self.assertTrue(result.fingerprint)

    def test_unchanged_findings_do_not_fire(self):
        findings = [
            {"severity": "error", "source": "github_actions", "message": "CI failed"},
        ]
        # First run fires
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=findings)
        # Second run with same findings does not fire
        result = run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=findings)
        self.assertFalse(result.fire)
        self.assertEqual(result.reason, "unchanged_findings")

    def test_changed_findings_fire_again(self):
        findings1 = [
            {"severity": "error", "source": "github_actions", "message": "CI failed"},
        ]
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=findings1)
        # New finding added
        findings2 = [
            {"severity": "error", "source": "github_actions", "message": "CI failed"},
            {"severity": "warn", "source": "aged_prs", "message": "PR stale"},
        ]
        result = run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=findings2)
        self.assertTrue(result.fire)
        self.assertEqual(result.reason, "new_findings")

    def test_resolved_findings_then_new_findings_fire(self):
        findings1 = [
            {"severity": "error", "source": "github_actions", "message": "CI failed"},
        ]
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=findings1)
        # Findings resolve
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=[])
        # New findings appear
        findings2 = [
            {"severity": "warn", "source": "django_check", "message": "warnings detected"},
        ]
        result = run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=findings2)
        self.assertTrue(result.fire)

    def test_info_findings_filtered_out(self):
        findings = [
            {"severity": "info", "source": "django_check", "message": "all good"},
        ]
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            findings_override=findings,
        )
        self.assertFalse(result.fire)
        self.assertEqual(result.reason, "no_actionable_findings")

    def test_mixed_findings_only_actionable_fire(self):
        findings = [
            {"severity": "error", "source": "github_actions", "message": "CI failed"},
            {"severity": "info", "source": "django_check", "message": "all good"},
            {"severity": "warn", "source": "aged_prs", "message": "stale PR"},
        ]
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            findings_override=findings,
        )
        self.assertTrue(result.fire)
        self.assertEqual(result.summary["finding_count"], 2)

    def test_firing_persists_fingerprint(self):
        store = StateStore(self.tmpdir, job_name=JOB_NAME)
        findings = [{"severity": "error", "source": "test", "message": "fail"}]
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=findings)
        state = store.load()
        self.assertTrue(state["fingerprint"])

    def test_no_fire_clears_fingerprint(self):
        store = StateStore(self.tmpdir, job_name=JOB_NAME)
        # Fire once
        findings = [{"severity": "error", "source": "test", "message": "fail"}]
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=findings)
        self.assertTrue(store.load().get("fingerprint"))
        # Now no findings → clears
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=[])
        self.assertEqual(store.load().get("fingerprint"), "")

    def test_fingerprint_stable_for_same_findings_different_order(self):
        findings_a = [
            {"severity": "error", "source": "github_actions", "message": "CI failed"},
            {"severity": "warn", "source": "aged_prs", "message": "stale PR"},
        ]
        findings_b = list(reversed(findings_a))
        result_a = run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=findings_a)
        # Reset state
        StateStore(self.tmpdir, job_name=JOB_NAME).clear()
        result_b = run_preflight(repo="owner/repo", state_dir=self.tmpdir, findings_override=findings_b)
        self.assertEqual(result_a.fingerprint, result_b.fingerprint)

    def test_multiple_findings_summary(self):
        findings = [
            {"severity": "error", "source": "github_actions", "message": "CI failed", "details": {"run_id": 1}},
            {"severity": "warn", "source": "aged_prs", "message": "stale PR", "details": {"pr_number": 42}},
        ]
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            findings_override=findings,
        )
        self.assertTrue(result.fire)
        self.assertEqual(len(result.summary["findings"]), 2)
        # Summary should not include details (minimal)
        for f in result.summary["findings"]:
            self.assertNotIn("details", f)
