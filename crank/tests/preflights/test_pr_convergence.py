# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for preflights.pr_convergence: no-work, actionable, unchanged, recovery, API failure, rate limit."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase

from preflights.common import (
    APIError,
    PreflightResult,
    RateLimitError,
    StateStore,
    TimeoutError as PreflightTimeoutError,
)
from preflights.pr_convergence import (
    _is_managed_pr,
    _extract_pr_state,
    _summarize_reviews,
    _summarize_checks,
    _is_actionable,
    compute_actionable_fingerprint,
    fetch_prs,
    fetch_pr_details,
    run_preflight,
    MANAGED_LABELS,
    JOB_NAME,
)


class IsManagedPrTests(SimpleTestCase):
    def test_sf_managed_label(self):
        pr = {"labels": [{"name": "sf-managed"}]}
        self.assertTrue(_is_managed_pr(pr))

    def test_factory_label(self):
        pr = {"labels": [{"name": "factory"}]}
        self.assertTrue(_is_managed_pr(pr))

    def test_auto_merge_label(self):
        pr = {"labels": [{"name": "auto-merge"}]}
        self.assertTrue(_is_managed_pr(pr))

    def test_non_managed_label(self):
        pr = {"labels": [{"name": "bug"}]}
        self.assertFalse(_is_managed_pr(pr))

    def test_no_labels(self):
        pr = {"labels": []}
        self.assertFalse(_is_managed_pr(pr))

    def test_empty_labels_list(self):
        pr = {"labels": [{}]}
        self.assertFalse(_is_managed_pr(pr))

    def test_multiple_labels_including_managed(self):
        pr = {"labels": [{"name": "bug"}, {"name": "sf-managed"}, {"name": "high-priority"}]}
        self.assertTrue(_is_managed_pr(pr))


class SummarizeReviewsTests(SimpleTestCase):
    def test_no_reviews(self):
        result = _summarize_reviews([])
        self.assertEqual(result, {"approved": 0, "changes_requested": 0, "pending": 0})

    def test_single_approval(self):
        reviews = [{"author": {"login": "r1"}, "state": "APPROVED"}]
        result = _summarize_reviews(reviews)
        self.assertEqual(result, {"approved": 1, "changes_requested": 0, "pending": 0})

    def test_changes_requested(self):
        reviews = [{"author": {"login": "r1"}, "state": "CHANGES_REQUESTED"}]
        result = _summarize_reviews(reviews)
        self.assertEqual(result, {"approved": 0, "changes_requested": 1, "pending": 0})

    def test_mixed_reviews_latest_wins(self):
        reviews = [
            {"author": {"login": "r1"}, "state": "CHANGES_REQUESTED"},
            {"author": {"login": "r1"}, "state": "APPROVED"},
            {"author": {"login": "r2"}, "state": "COMMENTED"},
        ]
        result = _summarize_reviews(reviews)
        self.assertEqual(result["approved"], 1)
        self.assertEqual(result["changes_requested"], 0)
        self.assertEqual(result["pending"], 1)

    def test_reviews_with_user_field_fallback(self):
        reviews = [{"user": {"login": "r1"}, "state": "APPROVED"}]
        result = _summarize_reviews(reviews)
        self.assertEqual(result["approved"], 1)


class SummarizeChecksTests(SimpleTestCase):
    def test_no_checks(self):
        result = _summarize_checks([])
        self.assertEqual(result, {"total": 0, "passing": 0, "failing": 0, "pending": 0})

    def test_all_passing(self):
        checks = [
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
        ]
        result = _summarize_checks(checks)
        self.assertEqual(result["passing"], 2)
        self.assertEqual(result["failing"], 0)

    def test_failing_checks(self):
        checks = [
            {"conclusion": "FAILURE", "status": "COMPLETED"},
            {"conclusion": "CANCELLED", "status": "COMPLETED"},
        ]
        result = _summarize_checks(checks)
        self.assertEqual(result["failing"], 2)

    def test_pending_checks(self):
        checks = [
            {"conclusion": None, "status": "IN_PROGRESS"},
            {"conclusion": None, "status": "QUEUED"},
        ]
        result = _summarize_checks(checks)
        self.assertEqual(result["pending"], 2)

    def test_mixed_checks(self):
        checks = [
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
            {"conclusion": "FAILURE", "status": "COMPLETED"},
            {"conclusion": None, "status": "IN_PROGRESS"},
        ]
        result = _summarize_checks(checks)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["passing"], 1)
        self.assertEqual(result["failing"], 1)
        self.assertEqual(result["pending"], 1)


class ExtractPrStateTests(SimpleTestCase):
    def test_extract_clean_pr(self):
        pr = {
            "number": 42,
            "head": {"sha": "abc123"},
            "mergeable": True,
            "mergeable_state": "clean",
            "labels": [{"name": "sf-managed"}, {"name": "ready-to-merge"}],
            "reviews": [{"author": {"login": "r1"}, "state": "APPROVED"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        }
        state = _extract_pr_state(pr)
        self.assertEqual(state["number"], 42)
        self.assertEqual(state["head_sha"], "abc123")
        self.assertTrue(state["mergeable"])
        self.assertEqual(state["mergeable_state"], "clean")
        self.assertIn("sf-managed", state["labels"])
        self.assertIn("ready-to-merge", state["labels"])
        self.assertEqual(state["review_state"]["approved"], 1)
        self.assertEqual(state["check_summary"]["passing"], 1)

    def test_extract_pr_with_missing_fields(self):
        state = _extract_pr_state({})
        self.assertIsNone(state["number"])
        self.assertEqual(state["head_sha"], "")
        self.assertIsNone(state["mergeable"])
        self.assertEqual(state["mergeable_state"], "")
        self.assertEqual(state["labels"], [])
        self.assertEqual(state["review_state"], {"approved": 0, "changes_requested": 0, "pending": 0})
        self.assertEqual(state["check_summary"], {"total": 0, "passing": 0, "failing": 0, "pending": 0})


class IsActionableTests(SimpleTestCase):
    def test_mergeable_false_is_actionable(self):
        state = {"mergeable": False, "mergeable_state": "dirty", "check_summary": {}, "review_state": {}, "labels": []}
        self.assertTrue(_is_actionable(state))

    def test_blocked_state_is_actionable(self):
        state = {"mergeable": True, "mergeable_state": "blocked", "check_summary": {}, "review_state": {}, "labels": []}
        self.assertTrue(_is_actionable(state))

    def test_failing_checks_is_actionable(self):
        state = {
            "mergeable": True, "mergeable_state": "clean",
            "check_summary": {"failing": 1},
            "review_state": {}, "labels": [],
        }
        self.assertTrue(_is_actionable(state))

    def test_changes_requested_is_actionable(self):
        state = {
            "mergeable": True, "mergeable_state": "clean",
            "check_summary": {},
            "review_state": {"changes_requested": 1},
            "labels": [],
        }
        self.assertTrue(_is_actionable(state))

    def test_blocked_label_is_actionable(self):
        state = {
            "mergeable": True, "mergeable_state": "clean",
            "check_summary": {},
            "review_state": {},
            "labels": ["blocked"],
        }
        self.assertTrue(_is_actionable(state))

    def test_do_not_merge_label_is_actionable(self):
        state = {
            "mergeable": True, "mergeable_state": "clean",
            "check_summary": {},
            "review_state": {},
            "labels": ["do-not-merge"],
        }
        self.assertTrue(_is_actionable(state))

    def test_clean_pr_not_actionable(self):
        state = {
            "mergeable": True, "mergeable_state": "clean",
            "check_summary": {"passing": 3, "failing": 0},
            "review_state": {"approved": 2, "changes_requested": 0},
            "labels": ["ready-to-merge"],
        }
        self.assertFalse(_is_actionable(state))


class ComputeActionableFingerprintTests(SimpleTestCase):
    def test_no_actionable_prs(self):
        states = [
            {"mergeable": True, "mergeable_state": "clean", "check_summary": {}, "review_state": {}, "labels": []},
        ]
        fp, actionable = compute_actionable_fingerprint(states)
        self.assertEqual(fp, "")
        self.assertEqual(len(actionable), 0)

    def test_one_actionable_pr(self):
        states = [
            {"number": 1, "mergeable": False, "mergeable_state": "dirty", "check_summary": {}, "review_state": {}, "labels": []},
            {"number": 2, "mergeable": True, "mergeable_state": "clean", "check_summary": {}, "review_state": {}, "labels": []},
        ]
        fp, actionable = compute_actionable_fingerprint(states)
        self.assertTrue(fp)
        self.assertEqual(len(actionable), 1)
        self.assertEqual(actionable[0]["number"], 1)

    def test_fingerprint_stable_across_order(self):
        states_a = [
            {"number": 1, "mergeable": False, "mergeable_state": "dirty", "check_summary": {}, "review_state": {}, "labels": []},
            {"number": 2, "mergeable": False, "mergeable_state": "dirty", "check_summary": {}, "review_state": {}, "labels": []},
        ]
        states_b = list(reversed(states_a))
        fp_a, _ = compute_actionable_fingerprint(states_a)
        fp_b, _ = compute_actionable_fingerprint(states_b)
        self.assertEqual(fp_a, fp_b)

    def test_different_states_different_fingerprint(self):
        states_a = [
            {"number": 1, "mergeable": False, "mergeable_state": "dirty", "check_summary": {}, "review_state": {}, "labels": []},
        ]
        states_b = [
            {"number": 1, "mergeable": True, "mergeable_state": "blocked", "check_summary": {"failing": 1}, "review_state": {}, "labels": []},
        ]
        fp_a, _ = compute_actionable_fingerprint(states_a)
        fp_b, _ = compute_actionable_fingerprint(states_b)
        self.assertNotEqual(fp_a, fp_b)


class RunPreflightTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_repo_returns_no_fire(self):
        result = run_preflight(repo=None, state_dir=self.tmpdir)
        self.assertFalse(result.fire)
        self.assertEqual(result.reason, "no_repo configured")
        self.assertIsNotNone(result.error)

    def test_no_managed_prs_returns_no_fire(self):
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            prs_override=[],
        )
        self.assertFalse(result.fire)
        self.assertEqual(result.reason, "no_managed_prs")

    def test_all_clean_prs_returns_no_fire(self):
        clean_pr = {
            "number": 42,
            "head": {"sha": "abc123"},
            "mergeable": True,
            "mergeable_state": "clean",
            "labels": [{"name": "sf-managed"}],
            "reviews": [{"author": {"login": "r1"}, "state": "APPROVED"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        }
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            prs_override=[clean_pr],
        )
        self.assertFalse(result.fire)
        self.assertEqual(result.reason, "no_actionable_prs")

    def test_actionable_pr_fires(self):
        conflict_pr = {
            "number": 99,
            "head": {"sha": "def456"},
            "mergeable": False,
            "mergeable_state": "dirty",
            "labels": [{"name": "sf-managed"}],
            "reviews": [{"author": {"login": "r1"}, "state": "APPROVED"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        }
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            prs_override=[conflict_pr],
        )
        self.assertTrue(result.fire)
        self.assertEqual(result.reason, "new_actionable_fingerprint")
        self.assertIn("actionable_pr_count", result.summary)
        self.assertEqual(result.summary["actionable_pr_count"], 1)
        self.assertTrue(result.fingerprint)

    def test_unchanged_actionable_state_does_not_fire(self):
        conflict_pr = {
            "number": 99,
            "head": {"sha": "def456"},
            "mergeable": False,
            "mergeable_state": "dirty",
            "labels": [{"name": "sf-managed"}],
            "reviews": [{"author": {"login": "r1"}, "state": "APPROVED"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        }
        # First run fires
        result1 = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            prs_override=[conflict_pr],
        )
        self.assertTrue(result1.fire)
        # Second run with same state does not fire
        result2 = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            prs_override=[conflict_pr],
        )
        self.assertFalse(result2.fire)
        self.assertEqual(result2.reason, "unchanged_actionable_state")

    def test_changed_state_fires_again(self):
        conflict_pr = {
            "number": 99,
            "head": {"sha": "def456"},
            "mergeable": False,
            "mergeable_state": "dirty",
            "labels": [{"name": "sf-managed"}],
            "reviews": [{"author": {"login": "r1"}, "state": "APPROVED"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        }
        # First run fires
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, prs_override=[conflict_pr])
        # Change head SHA → new fingerprint
        conflict_pr["head"] = {"sha": "new_sha"}
        conflict_pr["mergeable"] = True
        conflict_pr["mergeable_state"] = "clean"
        result = run_preflight(repo="owner/repo", state_dir=self.tmpdir, prs_override=[conflict_pr])
        # Now it's clean → no actionable → no fire
        self.assertFalse(result.fire)
        self.assertEqual(result.reason, "no_actionable_prs")

    def test_resolved_to_actionable_fires_again(self):
        clean_pr = {
            "number": 42,
            "head": {"sha": "abc123"},
            "mergeable": True,
            "mergeable_state": "clean",
            "labels": [{"name": "sf-managed"}],
            "reviews": [{"author": {"login": "r1"}, "state": "APPROVED"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        }
        # First run: clean → no fire, saves empty fingerprint
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, prs_override=[clean_pr])
        # Now PR becomes actionable (conflict introduced)
        conflict_pr = dict(clean_pr)
        conflict_pr["mergeable"] = False
        conflict_pr["mergeable_state"] = "dirty"
        result = run_preflight(repo="owner/repo", state_dir=self.tmpdir, prs_override=[conflict_pr])
        self.assertTrue(result.fire)
        self.assertEqual(result.reason, "new_actionable_fingerprint")

    def test_multiple_actionable_prs_fires(self):
        prs = [
            {
                "number": 1,
                "head": {"sha": "sha1"},
                "mergeable": False,
                "mergeable_state": "dirty",
                "labels": [{"name": "sf-managed"}],
                "reviews": [{"author": {"login": "r1"}, "state": "APPROVED"}],
                "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
            },
            {
                "number": 2,
                "head": {"sha": "sha2"},
                "mergeable": True,
                "mergeable_state": "blocked",
                "labels": [{"name": "factory"}],
                "reviews": [{"author": {"login": "r2"}, "state": "CHANGES_REQUESTED"}],
                "statusCheckRollup": [{"conclusion": "FAILURE", "status": "COMPLETED"}],
            },
        ]
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            prs_override=prs,
        )
        self.assertTrue(result.fire)
        self.assertEqual(result.summary["actionable_pr_count"], 2)

    def test_non_managed_prs_filtered_out(self):
        non_managed = {
            "number": 11,
            "head": {"sha": "pqr"},
            "mergeable": False,
            "mergeable_state": "dirty",
            "labels": [{"name": "bug"}],
            "reviews": [],
            "statusCheckRollup": [],
        }
        result = run_preflight(
            repo="owner/repo",
            state_dir=self.tmpdir,
            prs_override=[non_managed],
        )
        self.assertFalse(result.fire)
        self.assertEqual(result.reason, "no_managed_prs")

    def test_firing_persists_fingerprint(self):
        conflict_pr = {
            "number": 99,
            "head": {"sha": "def456"},
            "mergeable": False,
            "mergeable_state": "dirty",
            "labels": [{"name": "sf-managed"}],
            "reviews": [{"author": {"login": "r1"}, "state": "APPROVED"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        }
        store = StateStore(self.tmpdir, job_name=JOB_NAME)
        self.assertEqual(store.load(), {})
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, prs_override=[conflict_pr])
        state = store.load()
        self.assertTrue(state["fingerprint"])

    def test_no_fire_clears_fingerprint(self):
        conflict_pr = {
            "number": 99,
            "head": {"sha": "def456"},
            "mergeable": False,
            "mergeable_state": "dirty",
            "labels": [{"name": "sf-managed"}],
            "reviews": [{"author": {"login": "r1"}, "state": "APPROVED"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        }
        store = StateStore(self.tmpdir, job_name=JOB_NAME)
        # Fire once
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, prs_override=[conflict_pr])
        self.assertTrue(store.load().get("fingerprint"))
        # Now PR resolves → no actionable → saves empty fingerprint
        clean_pr = dict(conflict_pr)
        clean_pr["mergeable"] = True
        clean_pr["mergeable_state"] = "clean"
        run_preflight(repo="owner/repo", state_dir=self.tmpdir, prs_override=[clean_pr])
        self.assertEqual(store.load().get("fingerprint"), "")


class FetchPrsTests(SimpleTestCase):
    @patch("preflights.pr_convergence.run_gh_paginated")
    def test_fetch_open_managed_prs(self, mock_paginated):
        mock_paginated.return_value = [
            {"number": 1, "state": "open", "labels": [{"name": "sf-managed"}]},
            {"number": 2, "state": "open", "labels": [{"name": "bug"}]},
            {"number": 3, "state": "closed", "labels": [{"name": "sf-managed"}]},
        ]
        prs = fetch_prs("owner/repo")
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0]["number"], 1)

    @patch("preflights.pr_convergence.run_gh_paginated")
    def test_fetch_no_prs(self, mock_paginated):
        mock_paginated.return_value = []
        prs = fetch_prs("owner/repo")
        self.assertEqual(prs, [])


class FetchPrDetailsTests(SimpleTestCase):
    @patch("preflights.pr_convergence.run_gh_paginated")
    @patch("preflights.pr_convergence.run_gh")
    def test_fetch_details_with_checks(self, mock_gh, mock_paginated):
        mock_gh.side_effect = [
            {"number": 42, "head": {"sha": "abc"}, "mergeable": True, "mergeable_state": "clean"},
            {"check_runs": [{"conclusion": "SUCCESS", "status": "COMPLETED"}]},
        ]
        mock_paginated.return_value = [{"author": {"login": "r1"}, "state": "APPROVED"}]
        result = fetch_pr_details("owner/repo", 42)
        self.assertEqual(result["number"], 42)
        self.assertEqual(len(result["reviews"]), 1)
        self.assertEqual(len(result["statusCheckRollup"]), 1)

    @patch("preflights.pr_convergence.run_gh_paginated")
    @patch("preflights.pr_convergence.run_gh")
    def test_fetch_details_check_runs_api_failure_continues(self, mock_gh, mock_paginated):
        mock_gh.side_effect = [
            {"number": 42, "head": {"sha": "abc"}, "mergeable": True},
            APIError("check-runs not available"),
        ]
        mock_paginated.return_value = []
        result = fetch_pr_details("owner/repo", 42)
        self.assertEqual(result["statusCheckRollup"], [])


class RunPreflightAPIFailureTests(SimpleTestCase):
    @patch("preflights.pr_convergence.fetch_prs")
    def test_rate_limit_error_propagates(self, mock_fetch):
        mock_fetch.side_effect = RateLimitError("rate limited")
        with self.assertRaises(RateLimitError):
            run_preflight(repo="owner/repo", state_dir=tempfile.mkdtemp())

    @patch("preflights.pr_convergence.fetch_prs")
    def test_timeout_error_propagates(self, mock_fetch):
        mock_fetch.side_effect = PreflightTimeoutError("timed out")
        with self.assertRaises(PreflightTimeoutError):
            run_preflight(repo="owner/repo", state_dir=tempfile.mkdtemp())

    @patch("preflights.pr_convergence.fetch_prs")
    def test_api_error_propagates(self, mock_fetch):
        mock_fetch.side_effect = APIError("server error")
        with self.assertRaises(APIError):
            run_preflight(repo="owner/repo", state_dir=tempfile.mkdtemp())
