# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for preflights.common: run_gh_paginated, emit_and_exit, emit_error_and_exit."""
import json
import os
import subprocess
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
    compute_fingerprint,
    run_gh,
    run_gh_paginated,
    emit_and_exit,
    emit_error_and_exit,
)


class RunGhPaginatedTests(SimpleTestCase):
    @patch("preflights.common.subprocess.run")
    def test_successful_list_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"number": 1}, {"number": 2}]',
            stderr="",
        )
        result = run_gh_paginated("repos/owner/repo/pulls", repo="owner/repo")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["number"], 1)

    @patch("preflights.common.subprocess.run")
    def test_successful_single_object_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"id": 42}',
            stderr="",
        )
        result = run_gh_paginated("repos/owner/repo/pulls/42")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], 42)

    @patch("preflights.common.subprocess.run")
    def test_empty_output_returns_empty_list(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )
        result = run_gh_paginated("endpoint")
        self.assertEqual(result, [])

    @patch("preflights.common.subprocess.run")
    def test_non_zero_exit_raises_api_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="gh: error: not found",
        )
        with self.assertRaises(APIError):
            run_gh_paginated("bad/endpoint")

    @patch("preflights.common.subprocess.run")
    def test_rate_limit_raises_rate_limit_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="HTTP 403: rate limit exceeded",
        )
        with self.assertRaises(RateLimitError):
            run_gh_paginated("endpoint")

    @patch("preflights.common.subprocess.run")
    def test_timeout_raises_timeout_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=10)
        with self.assertRaises(PreflightTimeoutError):
            run_gh_paginated("endpoint", timeout=1, max_pages=10)

    @patch("preflights.common.subprocess.run")
    def test_line_delimited_json(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"id": 1}\n{"id": 2}\n{"id": 3}',
            stderr="",
        )
        result = run_gh_paginated("endpoint")
        self.assertEqual(len(result), 3)

    @patch("preflights.common.subprocess.run")
    def test_line_delimited_json_with_arrays(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"id": 1}]\n[{"id": 2}]',
            stderr="",
        )
        result = run_gh_paginated("endpoint")
        self.assertEqual(len(result), 2)

    @patch("preflights.common.subprocess.run")
    def test_line_delimited_json_skips_invalid_lines(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"id": 1}\nnot json\n{"id": 2}',
            stderr="",
        )
        result = run_gh_paginated("endpoint")
        self.assertEqual(len(result), 2)

    @patch("preflights.common.subprocess.run")
    def test_unparseable_output_raises_api_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="totally not json at all",
            stderr="",
        )
        with self.assertRaises(APIError):
            run_gh_paginated("endpoint")

    @patch("preflights.common.subprocess.run")
    def test_repo_env_passed_to_gh(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"ok": true}]',
            stderr="",
        )
        run_gh_paginated("endpoint", repo="owner/repo")
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--repo", cmd)
        env = mock_run.call_args.kwargs.get("env")
        self.assertIsNotNone(env)
        self.assertEqual(env["GH_REPO"], "owner/repo")

    @patch("preflights.common.subprocess.run")
    def test_jq_filter_passed_to_gh(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"ok": true}]',
            stderr="",
        )
        run_gh_paginated("endpoint", jq_filter=".items[]")
        cmd = mock_run.call_args[0][0]
        self.assertIn("--jq", cmd)


class EmitAndExitTests(SimpleTestCase):
    def test_emit_and_exit_exits_zero(self):
        result = PreflightResult(fire=False, reason="test")
        with self.assertRaises(SystemExit) as ctx:
            emit_and_exit(result)
        self.assertEqual(ctx.exception.code, 0)

    def test_emit_and_exit_prints_json(self):
        result = PreflightResult(fire=True, reason="fired", summary={"count": 1})
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                emit_and_exit(result)
            mock_print.assert_called_once_with(result.to_json())


class EmitErrorAndExitTests(SimpleTestCase):
    def test_rate_limit_error_exits_with_code_3(self):
        err = RateLimitError("rate limited")
        with self.assertRaises(SystemExit) as ctx:
            emit_error_and_exit(err, "test-job")
        self.assertEqual(ctx.exception.code, 3)

    def test_timeout_error_exits_with_code_4(self):
        err = PreflightTimeoutError("timed out")
        with self.assertRaises(SystemExit) as ctx:
            emit_error_and_exit(err, "test-job")
        self.assertEqual(ctx.exception.code, 4)

    def test_api_error_exits_with_code_5(self):
        err = APIError("server error")
        with self.assertRaises(SystemExit) as ctx:
            emit_error_and_exit(err, "test-job")
        self.assertEqual(ctx.exception.code, 5)

    def test_generic_preflight_error_exits_with_code_2(self):
        err = PreflightError("something broke")
        with self.assertRaises(SystemExit) as ctx:
            emit_error_and_exit(err, "test-job")
        self.assertEqual(ctx.exception.code, 2)

    def test_emits_error_in_json(self):
        err = APIError("server error")
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                emit_error_and_exit(err, "test-job")
            printed = mock_print.call_args[0][0]
            parsed = json.loads(printed)
            self.assertFalse(parsed["fire"])
            self.assertIn("APIError", parsed["error"])
            self.assertIn("server error", parsed["error"])
