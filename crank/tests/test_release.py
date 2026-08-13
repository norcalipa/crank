# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Unit tests for the staff-only release diagnostics helpers."""

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from crank.models.job import JobListing, JobSourceCatalog
from crank.release import (
    _GIT_SHA_ENV_VARS,
    config_modes,
    counts,
    diagnostics,
    frontend_build_id,
    git_sha,
    migration_status_summary,
)


@contextmanager
def _git_env(**overrides):
    base = {name: "" for name in _GIT_SHA_ENV_VARS}
    base.update(overrides)
    with patch.dict(os.environ, base, clear=False):
        yield


class GitShaTests(SimpleTestCase):
    def test_prefers_first_set_variable(self):
        with _git_env(GIT_SHA="abc123def456", SOURCE_VERSION="feedface"):
            self.assertEqual(git_sha(), "abc123def456")

    def test_falls_back_to_later_variable(self):
        with _git_env(SOURCE_VERSION="feedface"):
            self.assertEqual(git_sha(), "feedface")

    def test_unknown_when_none_set(self):
        with _git_env():
            self.assertEqual(git_sha(), "unknown")

    def test_rejects_unsafe_value(self):
        with _git_env(GIT_SHA="abc$def; rm -rf /"):
            self.assertEqual(git_sha(), "unknown")

    def test_trims_whitespace(self):
        with _git_env(GIT_SHA="  abc123  "):
            self.assertEqual(git_sha(), "abc123")


class FrontendBuildIdTests(SimpleTestCase):
    def _write_manifest(self, content):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, "manifest.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_extracts_contenthash_from_main_js(self):
        path = self._write_manifest(
            json.dumps({"main.js": "main.1a2b3c4d5e6f.js", "jobsearch.js": "jobsearch.zzz.js"})
        )
        with override_settings(MANIFEST_LOADER={"MANIFEST_PATH": path}):
            self.assertEqual(frontend_build_id(), "1a2b3c4d5e6f")

    def test_falls_back_to_main_key(self):
        path = self._write_manifest(json.dumps({"main": "main.abcdef012345.js"}))
        with override_settings(MANIFEST_LOADER={"MANIFEST_PATH": path}):
            self.assertEqual(frontend_build_id(), "abcdef012345")

    def test_unknown_when_manifest_missing(self):
        with override_settings(MANIFEST_LOADER={"MANIFEST_PATH": "/nonexistent/manifest.json"}):
            self.assertEqual(frontend_build_id(), "unknown")

    def test_unknown_when_no_main_entry(self):
        path = self._write_manifest(json.dumps({"jobsearch.js": "jobsearch.zzz.js"}))
        with override_settings(MANIFEST_LOADER={"MANIFEST_PATH": path}):
            self.assertEqual(frontend_build_id(), "unknown")

    def test_unknown_when_manifest_loader_unconfigured(self):
        # With no MANIFEST_PATH the helper derives a path from BASE_DIR;
        # point BASE_DIR at a location that has no manifest so it fails closed.
        with override_settings(MANIFEST_LOADER={}, BASE_DIR="/nonexistent/crank"):
            self.assertEqual(frontend_build_id(), "unknown")

    def test_unknown_when_manifest_is_invalid_json(self):
        path = self._write_manifest("{not valid json")
        with override_settings(MANIFEST_LOADER={"MANIFEST_PATH": path}):
            self.assertEqual(frontend_build_id(), "unknown")


class MigrationStatusSummaryTests(SimpleTestCase):
    @patch("crank.release.MigrationExecutor")
    def test_clean_when_no_pending(self, executor_class):
        executor = executor_class.return_value
        executor.loader.applied_migrations = [1, 2, 3]
        executor.loader.graph.leaf_nodes.return_value = [("crank", "0026")]
        executor.migration_plan.return_value = []
        self.assertEqual(
            migration_status_summary(),
            {"applied_count": 3, "pending_count": 0, "status": "clean"},
        )

    @patch("crank.release.MigrationExecutor")
    def test_pending_when_plan_nonempty(self, executor_class):
        executor = executor_class.return_value
        executor.loader.applied_migrations = [1]
        executor.loader.graph.leaf_nodes.return_value = [("crank", "0026")]
        executor.migration_plan.return_value = [Mock(), Mock()]
        self.assertEqual(
            migration_status_summary(),
            {"applied_count": 1, "pending_count": 2, "status": "pending"},
        )

    @patch("crank.release.MigrationExecutor", side_effect=RuntimeError("DB down"))
    def test_error_when_database_unreachable(self, executor_class):
        self.assertEqual(
            migration_status_summary(),
            {"applied_count": None, "pending_count": None, "status": "error"},
        )


class ConfigModesTests(SimpleTestCase):
    @override_settings(
        JOB_SEARCH_PROVIDER="usa_jobs",
        LLM_PROVIDER="openai",
        JOB_PIPELINE_ENABLED=True,
        CRAWL_CRON_ENABLED=True,
    )
    def test_reports_non_secret_modes(self):
        self.assertEqual(
            config_modes(),
            {
                "job_search_provider": "usa_jobs",
                "llm_configured": True,
                "job_pipeline_enabled": True,
                "crawl_scheduling_enabled": True,
            },
        )

    @override_settings(LLM_PROVIDER="", LLM_API_KEY="super-secret-key")
    def test_llm_configured_is_bool_only(self):
        modes = config_modes()
        self.assertFalse(modes["llm_configured"])
        self.assertNotIn("super-secret-key", json.dumps(modes))
        self.assertNotIn("api_key", json.dumps(modes))


class CountsTests(TestCase):
    def test_counts_active_only(self):
        source = JobSourceCatalog.objects.create(
            name="Test jobs",
            adapter_key="test.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        now = timezone.now()
        JobListing.objects.create(
            source=source,
            canonical_url="https://jobs.example.test/1",
            employer_name="Acme",
            title="Engineer",
            first_seen_at=now,
            last_seen_at=now,
        )
        JobListing.objects.create(
            source=source,
            canonical_url="https://jobs.example.test/2",
            employer_name="Acme",
            title="Closed role",
            first_seen_at=now,
            last_seen_at=now,
            status=JobListing.Status.CLOSED,
        )
        self.assertEqual(
            counts(),
            {"job_source_catalog_count": 1, "active_job_listing_count": 1},
        )


class DiagnosticsTests(TestCase):
    @override_settings(
        LLM_API_KEY="sk-1234567890",
        YELP_API_KEY="yelp-secret",
        FIRECRAWL_API_KEY="fc-secret",
        USAJOBS_AUTH_KEY="usajobs-secret",
        SECRET_KEY="django-secret",
    )
    def test_diagnostics_never_expose_secrets(self):
        blob = json.dumps(diagnostics(), sort_keys=True)
        for secret in (
            "sk-1234567890",
            "yelp-secret",
            "fc-secret",
            "usajobs-secret",
            "django-secret",
        ):
            self.assertNotIn(secret, blob)
        for forbidden in ("api_key", "auth_key", "password", "secret_key"):
            self.assertNotIn(forbidden, blob.lower())

    def test_diagnostics_shape(self):
        data = diagnostics()
        self.assertEqual(
            set(data),
            {"git_sha", "frontend_build_id", "migrations", "config", "counts"},
        )
        self.assertEqual(
            set(data["config"]),
            {
                "job_search_provider",
                "llm_configured",
                "job_pipeline_enabled",
                "crawl_scheduling_enabled",
            },
        )
