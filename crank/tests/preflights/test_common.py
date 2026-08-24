# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for preflights.common: StateStore, fingerprint, PreflightResult, error hierarchy."""
import json
import os
import tempfile
from pathlib import Path
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
)


class PreflightResultTests(SimpleTestCase):
    def test_fire_true_result_json(self):
        result = PreflightResult(
            fire=True,
            reason="test_reason",
            summary={"count": 1},
            fingerprint="abc123",
        )
        parsed = json.loads(result.to_json())
        self.assertTrue(parsed["fire"])
        self.assertEqual(parsed["reason"], "test_reason")
        self.assertEqual(parsed["summary"], {"count": 1})
        self.assertEqual(parsed["fingerprint"], "abc123")
        self.assertIsNone(parsed["error"])

    def test_fire_false_result_json(self):
        result = PreflightResult(fire=False, reason="no_work")
        parsed = json.loads(result.to_json())
        self.assertFalse(parsed["fire"])
        self.assertEqual(parsed["reason"], "no_work")
        self.assertEqual(parsed["summary"], {})

    def test_error_result_json(self):
        result = PreflightResult(
            fire=False,
            reason="error",
            error="something broke",
        )
        parsed = json.loads(result.to_json())
        self.assertFalse(parsed["fire"])
        self.assertEqual(parsed["error"], "something broke")

    def test_emit_writes_json_to_stdout(self):
        result = PreflightResult(fire=False, reason="test")
        with patch("builtins.print") as mock_print:
            result.emit()
            mock_print.assert_called_once_with(result.to_json())


class ComputeFingerprintTests(SimpleTestCase):
    def test_stable_fingerprint(self):
        fp1 = compute_fingerprint({"a": 1, "b": 2})
        fp2 = compute_fingerprint({"b": 2, "a": 1})
        self.assertEqual(fp1, fp2)

    def test_different_data_different_fingerprint(self):
        fp1 = compute_fingerprint({"a": 1})
        fp2 = compute_fingerprint({"a": 2})
        self.assertNotEqual(fp1, fp2)

    def test_empty_data_fingerprint(self):
        fp = compute_fingerprint([])
        self.assertEqual(len(fp), 16)

    def test_fingerprint_is_hex(self):
        fp = compute_fingerprint({"test": True})
        self.assertTrue(all(c in "0123456789abcdef" for c in fp))


class StateStoreTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = StateStore(self.tmpdir, job_name="test-job")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_returns_empty_when_no_state(self):
        self.assertEqual(self.store.load(), {})

    def test_save_and_load_roundtrip(self):
        self.store.save("fp123", {"count": 2})
        state = self.store.load()
        self.assertEqual(state["fingerprint"], "fp123")
        self.assertEqual(state["summary"], {"count": 2})
        self.assertIn("updated_at", state)

    def test_has_changed_true_when_different(self):
        self.store.save("old_fp", {})
        self.assertTrue(self.store.has_changed("new_fp"))

    def test_has_changed_false_when_same(self):
        self.store.save("same_fp", {})
        self.assertFalse(self.store.has_changed("same_fp"))

    def test_has_changed_true_when_no_state(self):
        self.assertTrue(self.store.has_changed("anything"))

    def test_clear_removes_state_file(self):
        self.store.save("fp", {})
        self.assertTrue(self.store.state_file.exists())
        self.store.clear()
        self.assertFalse(self.store.state_file.exists())

    def test_clear_is_noop_when_no_state(self):
        # Should not raise
        self.store.clear()
        self.assertFalse(self.store.state_file.exists())

    def test_load_returns_empty_on_corrupt_json(self):
        self.store.state_dir.mkdir(parents=True, exist_ok=True)
        self.store.state_file.write_text("{invalid json")
        self.assertEqual(self.store.load(), {})

    def test_save_creates_state_dir(self):
        nested = os.path.join(self.tmpdir, "nested", "deep")
        store = StateStore(nested, job_name="test")
        store.save("fp", {})
        self.assertTrue(store.state_file.exists())

    def test_save_overwrites_previous(self):
        self.store.save("fp1", {"count": 1})
        self.store.save("fp2", {"count": 2})
        state = self.store.load()
        self.assertEqual(state["fingerprint"], "fp2")
        self.assertEqual(state["summary"], {"count": 2})


class RunGhTests(SimpleTestCase):
    @patch("preflights.common.subprocess.run")
    def test_successful_json_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"key": "value"}',
            stderr="",
        )
        result = run_gh("api", "repos/owner/repo/pulls")
        self.assertEqual(result, {"key": "value"})

    @patch("preflights.common.subprocess.run")
    def test_empty_output_returns_empty_dict(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )
        result = run_gh("api", "some/endpoint")
        self.assertEqual(result, {})

    @patch("preflights.common.subprocess.run")
    def test_non_zero_exit_raises_api_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="gh: error: not found",
        )
        with self.assertRaises(APIError):
            run_gh("api", "bad/endpoint")

    @patch("preflights.common.subprocess.run")
    def test_rate_limit_raises_rate_limit_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="HTTP 403: rate limit exceeded",
        )
        with self.assertRaises(RateLimitError):
            run_gh("api", "endpoint")

    @patch("preflights.common.subprocess.run")
    def test_timeout_raises_timeout_error(self, mock_run):
        import subprocess as real_subprocess
        mock_run.side_effect = real_subprocess.TimeoutExpired(cmd="gh", timeout=5)
        with self.assertRaises(PreflightTimeoutError):
            run_gh("api", "endpoint", timeout=5)

    @patch("preflights.common.subprocess.run")
    def test_non_json_output_raises_api_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not json at all",
            stderr="",
        )
        with self.assertRaises(APIError):
            run_gh("api", "endpoint")

    @patch("preflights.common.subprocess.run")
    def test_repo_env_passed_to_gh(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"ok": true}',
            stderr="",
        )
        run_gh("api", "endpoint", repo="owner/repo")
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--repo", cmd)
        env = mock_run.call_args.kwargs.get("env")
        self.assertIsNotNone(env)
        self.assertEqual(env["GH_REPO"], "owner/repo")


class ErrorHierarchyTests(SimpleTestCase):
    def test_all_errors_are_preflight_errors(self):
        for err_cls in [APIError, RateLimitError, PreflightTimeoutError]:
            self.assertTrue(issubclass(err_cls, PreflightError))

    def test_exit_codes_are_distinct(self):
        self.assertEqual(PreflightError.exit_code, 2)
        self.assertEqual(RateLimitError.exit_code, 3)
        self.assertEqual(PreflightTimeoutError.exit_code, 4)
        self.assertEqual(APIError.exit_code, 5)
