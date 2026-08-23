# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for preflight main() entry points and remaining uncovered paths."""
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase

from preflights.common import (
    APIError,
    PreflightError,
    PreflightResult,
    RateLimitError,
    StateStore,
    TimeoutError as PreflightTimeoutError,
)
from preflights.pr_convergence import (
    run_preflight,
    fetch_pr_details,
    main as pr_main,
)
from preflights.watchdog import (
    _run_subprocess,
    audit_django_checks,
    audit_open_prs_aged,
    collect_findings,
    run_preflight as watchdog_run_preflight,
    main as watchdog_main,
)


class PrConvergenceMainTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("preflights.pr_convergence.DEFAULT_REPO", "owner/repo")
    @patch("preflights.pr_convergence.fetch_prs")
    def test_main_no_managed_prs_exits_zero(self, mock_fetch):
        mock_fetch.return_value = []
        with self.assertRaises(SystemExit) as ctx:
            pr_main()
        self.assertEqual(ctx.exception.code, 0)

    @patch("preflights.pr_convergence.DEFAULT_REPO", "")
    def test_main_no_repo_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            pr_main()
        self.assertEqual(ctx.exception.code, 0)

    @patch("preflights.pr_convergence.run_preflight")
    def test_main_rate_limit_exits_3(self, mock_run):
        mock_run.side_effect = RateLimitError("rate limited")
        with self.assertRaises(SystemExit) as ctx:
            pr_main()
        self.assertEqual(ctx.exception.code, 3)

    @patch("preflights.pr_convergence.run_preflight")
    def test_main_timeout_exits_4(self, mock_run):
        mock_run.side_effect = PreflightTimeoutError("timed out")
        with self.assertRaises(SystemExit) as ctx:
            pr_main()
        self.assertEqual(ctx.exception.code, 4)

    @patch("preflights.pr_convergence.run_preflight")
    def test_main_api_error_exits_5(self, mock_run):
        mock_run.side_effect = APIError("server error")
        with self.assertRaises(SystemExit) as ctx:
            pr_main()
        self.assertEqual(ctx.exception.code, 5)

    @patch("preflights.pr_convergence.run_preflight")
    def test_main_unexpected_error_exits_2(self, mock_run):
        mock_run.side_effect = ValueError("unexpected")
        with self.assertRaises(SystemExit) as ctx:
            pr_main()
        self.assertEqual(ctx.exception.code, 2)

    @patch("preflights.pr_convergence.run_preflight")
    def test_main_generic_preflight_error_exits_2(self, mock_run):
        mock_run.side_effect = PreflightError("generic")
        with self.assertRaises(SystemExit) as ctx:
            pr_main()
        self.assertEqual(ctx.exception.code, 2)


class PrConvergenceDetailsOverrideTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_details_override_used(self):
        """Test that details_override is used when provided."""
        pr = {"number": 42, "labels": [{"name": "sf-managed"}]}
        details = {
            "number": 42,
            "head": {"sha": "abc123"},
            "mergeable": False,
            "mergeable_state": "dirty",
            "labels": [{"name": "sf-managed"}],
            "reviews": [{"author": {"login": "r1"}, "state": "APPROVED"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        }
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            prs_override=[pr],
            details_override={42: details},
        )
        self.assertTrue(result.fire)
        self.assertEqual(result.reason, "new_actionable_fingerprint")

    def test_prs_override_with_details_from_pr(self):
        """When prs_override is set and no details_override, use PR dict directly."""
        pr = {
            "number": 42,
            "labels": [{"name": "sf-managed"}],
            "head": {"sha": "abc123"},
            "mergeable": False,
            "mergeable_state": "dirty",
            "reviews": [{"author": {"login": "r1"}, "state": "APPROVED"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        }
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            prs_override=[pr],
        )
        self.assertTrue(result.fire)


class WatchdogMainTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("preflights.watchdog.DEFAULT_REPO", "owner/repo")
    @patch("preflights.watchdog.collect_findings")
    def test_main_no_findings_exits_zero(self, mock_collect):
        mock_collect.return_value = []
        with self.assertRaises(SystemExit) as ctx:
            watchdog_main()
        self.assertEqual(ctx.exception.code, 0)

    @patch("preflights.watchdog.run_preflight")
    def test_main_rate_limit_exits_3(self, mock_run):
        mock_run.side_effect = RateLimitError("rate limited")
        with self.assertRaises(SystemExit) as ctx:
            watchdog_main()
        self.assertEqual(ctx.exception.code, 3)

    @patch("preflights.watchdog.run_preflight")
    def test_main_timeout_exits_4(self, mock_run):
        mock_run.side_effect = PreflightTimeoutError("timed out")
        with self.assertRaises(SystemExit) as ctx:
            watchdog_main()
        self.assertEqual(ctx.exception.code, 4)

    @patch("preflights.watchdog.run_preflight")
    def test_main_api_error_exits_5(self, mock_run):
        mock_run.side_effect = APIError("server error")
        with self.assertRaises(SystemExit) as ctx:
            watchdog_main()
        self.assertEqual(ctx.exception.code, 5)

    @patch("preflights.watchdog.run_preflight")
    def test_main_unexpected_error_exits_2(self, mock_run):
        mock_run.side_effect = ValueError("unexpected")
        with self.assertRaises(SystemExit) as ctx:
            watchdog_main()
        self.assertEqual(ctx.exception.code, 2)

    @patch("preflights.watchdog.run_preflight")
    def test_main_generic_preflight_error_exits_2(self, mock_run):
        mock_run.side_effect = PreflightError("generic")
        with self.assertRaises(SystemExit) as ctx:
            watchdog_main()
        self.assertEqual(ctx.exception.code, 2)


class RunSubprocessTests(SimpleTestCase):
    def test_successful_command(self):
        rc, stdout, stderr = _run_subprocess(["echo", "hello"])
        self.assertEqual(rc, 0)
        self.assertIn("hello", stdout)

    def test_failing_command(self):
        rc, stdout, stderr = _run_subprocess(["false"])
        self.assertEqual(rc, 1)

    def test_timeout(self):
        rc, stdout, stderr = _run_subprocess(["sleep", "10"], timeout=1)
        self.assertEqual(rc, -1)
        self.assertIn("timeout", stderr)


class AuditDjangoChecksTests(SimpleTestCase):
    @patch("preflights.watchdog._run_subprocess")
    def test_checks_pass_clean(self, mock_subproc):
        mock_subproc.return_value = (0, "System check identified no issues.", "")
        findings = audit_django_checks()
        self.assertEqual(findings, [])

    @patch("preflights.watchdog._run_subprocess")
    def test_checks_fail_with_error(self, mock_subproc):
        mock_subproc.return_value = (1, "", "Error: some error")
        findings = audit_django_checks()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "error")
        self.assertEqual(findings[0]["source"], "django_check")

    @patch("preflights.watchdog._run_subprocess")
    def test_checks_with_warnings(self, mock_subproc):
        mock_subproc.return_value = (0, "some warning text here", "")
        findings = audit_django_checks()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warn")
        self.assertEqual(findings[0]["source"], "django_check")


class AuditOpenPrsAgedTests(SimpleTestCase):
    @patch("preflights.watchdog.run_gh")
    def test_no_prs(self, mock_gh):
        mock_gh.return_value = []
        findings = audit_open_prs_aged("owner/repo")
        self.assertEqual(findings, [])

    @patch("preflights.watchdog.run_gh")
    def test_stale_pr_warning(self, mock_gh):
        import datetime
        old_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)).isoformat()
        mock_gh.return_value = [
            {"number": 42, "state": "open", "updated_at": old_date, "title": "Fix something"},
        ]
        findings = audit_open_prs_aged("owner/repo")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warn")
        self.assertEqual(findings[0]["details"]["pr_number"], 42)

    @patch("preflights.watchdog.run_gh")
    def test_recent_pr_no_warning(self, mock_gh):
        import datetime
        recent_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).isoformat()
        mock_gh.return_value = [
            {"number": 42, "state": "open", "updated_at": recent_date, "title": "Fix something"},
        ]
        findings = audit_open_prs_aged("owner/repo")
        self.assertEqual(findings, [])

    @patch("preflights.watchdog.run_gh")
    def test_closed_pr_ignored(self, mock_gh):
        import datetime
        old_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)).isoformat()
        mock_gh.return_value = [
            {"number": 42, "state": "closed", "updated_at": old_date, "title": "Fix something"},
        ]
        findings = audit_open_prs_aged("owner/repo")
        self.assertEqual(findings, [])

    @patch("preflights.watchdog.run_gh")
    def test_missing_updated_at_ignored(self, mock_gh):
        mock_gh.return_value = [
            {"number": 42, "state": "open", "title": "Fix something"},
        ]
        findings = audit_open_prs_aged("owner/repo")
        self.assertEqual(findings, [])

    @patch("preflights.watchdog.run_gh")
    def test_invalid_date_ignored(self, mock_gh):
        mock_gh.return_value = [
            {"number": 42, "state": "open", "updated_at": "not-a-date", "title": "Fix something"},
        ]
        findings = audit_open_prs_aged("owner/repo")
        self.assertEqual(findings, [])

    @patch("preflights.watchdog.run_gh")
    def test_api_failure_returns_warning(self, mock_gh):
        mock_gh.side_effect = APIError("server error")
        findings = audit_open_prs_aged("owner/repo")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warn")
        self.assertEqual(findings[0]["source"], "aged_prs")

    @patch("preflights.watchdog.run_gh")
    def test_rate_limit_returns_warning(self, mock_gh):
        mock_gh.side_effect = RateLimitError("rate limited")
        findings = audit_open_prs_aged("owner/repo")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warn")

    @patch("preflights.watchdog.run_gh")
    def test_non_list_response(self, mock_gh):
        mock_gh.return_value = {"not": "a list"}
        findings = audit_open_prs_aged("owner/repo")
        self.assertEqual(findings, [])


class CollectFindingsIntegrationTests(SimpleTestCase):
    """Test collect_findings with real audit functions (mocked subprocess/API)."""

    @patch("preflights.watchdog.audit_open_prs_aged")
    @patch("preflights.watchdog.audit_django_checks")
    @patch("preflights.watchdog.audit_github_actions")
    def test_all_audits_called(self, mock_gh_actions, mock_django, mock_aged):
        mock_gh_actions.return_value = [{"severity": "error", "source": "gh"}]
        mock_django.return_value = [{"severity": "warn", "source": "django"}]
        mock_aged.return_value = [{"severity": "warn", "source": "aged"}]
        findings = collect_findings("owner/repo")
        self.assertEqual(len(findings), 3)
        mock_gh_actions.assert_called_once_with("owner/repo")
        mock_django.assert_called_once()
        mock_aged.assert_called_once_with("owner/repo")

    @patch("preflights.watchdog.audit_open_prs_aged")
    @patch("preflights.watchdog.audit_django_checks")
    @patch("preflights.watchdog.audit_github_actions")
    def test_no_repo_skips_github_audits(self, mock_gh_actions, mock_django, mock_aged):
        mock_django.return_value = []
        findings = collect_findings("")
        self.assertEqual(findings, [])
        mock_gh_actions.assert_not_called()
        mock_aged.assert_not_called()
        mock_django.assert_called_once()
