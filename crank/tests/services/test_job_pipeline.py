# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for bounded periodic job ingestion and matching."""

from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crank.agents.jobs.base import JobSourceQuery

import yaml
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from crank.agents.jobs.ingest import JobIngestResult
from crank.models import AgentRun, JobSourceCatalog, UserPreference
from crank.services import agent_runs
from crank.services.job_pipeline import (
    JobPipelineError,
    _active_listings,
    _adapter_for,
    _has_active_preferences,
    _is_meaningful,
    _resolve_source_listings,
    _run_user,
    _setting,
    _source_query,
    run_job_pipeline,
)


class JobPipelineServiceTests(TestCase):
    def test_helper_options_and_adapter_selection(self):
        query = JobSourceQuery(max_listings=2)
        source = SimpleNamespace(pk=7)
        adapter = object()
        self.assertIs(_source_query({"query": query}, 500), query)
        self.assertEqual(_source_query({}, 2).max_listings, 2)
        self.assertIs(_adapter_for(source, {"adapter": {7: adapter}}), adapter)
        self.assertIs(_adapter_for(source, {"adapter": lambda value: value}), source)
        self.assertEqual(
            _setting({"deadline_seconds": 4}, "JOB_PIPELINE_DEADLINE_SECONDS", 9),
            4,
        )
        self.assertEqual(
            _setting({"job_pipeline_deadline_seconds": 5}, "JOB_PIPELINE_DEADLINE_SECONDS", 9),
            5,
        )
        self.assertEqual(
            _setting({"JOB_PIPELINE_DEADLINE_SECONDS": 6}, "JOB_PIPELINE_DEADLINE_SECONDS", 9),
            6,
        )
        self.assertTrue(_is_meaningful({"notes": "active"}))
        self.assertFalse(_is_meaningful({"notes": ""}))
        self.assertFalse(_is_meaningful(None))
        self.assertTrue(_is_meaningful(1))
        self.assertTrue(_is_meaningful(["remote"]))
        self.assertFalse(_has_active_preferences({"notes": ""}))
        self.assertTrue(_has_active_preferences(["remote"], []))

    def setUp(self):
        self.run = AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.RUNNING,
        )

    def source(self, name, *, enabled=True, approved=True):
        return JobSourceCatalog.objects.create(
            name=name,
            adapter_key="fake.v1",
            base_url="https://jobs.example.test",
            enabled=enabled,
            approval_state=(
                JobSourceCatalog.ApprovalState.APPROVED
                if approved
                else JobSourceCatalog.ApprovalState.PENDING
            ),
        )

    def preference(self, username, *, active=True, values=None):
        user = User.objects.create_user(username, is_active=active)
        return UserPreference.objects.create(
            user=user,
            preferences=(
                {"notes": "remote engineering"} if values is None else values
            ),
        )

    @override_settings(JOB_PIPELINE_DEADLINE_SECONDS=300)
    def test_no_sources_and_no_users_returns_zero_counts(self):
        with patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            counts = run_job_pipeline(self.run)
        self.assertEqual(counts["sources_total"], 0)
        self.assertEqual(counts["users_total"], 0)
        self.assertFalse(counts["deadline_reached"])

    def test_success_ingests_resolves_and_matches(self):
        self.source("good")
        self.preference("alice")
        result = JobIngestResult(ingested=2, updated=1)
        with patch("crank.services.job_pipeline.ingest_jobs", return_value=result), patch(
            "crank.services.job_pipeline._resolve_source_listings", return_value=(2, 0)
        ), patch("crank.services.job_pipeline._active_listings", return_value=[object()]), patch(
            "crank.services.job_pipeline._run_user", return_value=3
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            counts = run_job_pipeline(self.run)
        self.assertEqual(counts["sources_succeeded"], 1)
        self.assertEqual(counts["listings_ingested"], 2)
        self.assertEqual(counts["listings_updated"], 1)
        self.assertEqual(counts["employers_resolved"], 2)
        self.assertEqual(counts["users_succeeded"], 1)
        self.assertEqual(counts["matches_persisted"], 3)

    def test_source_exception_does_not_stop_other_sources(self):
        self.source("raised")
        self.source("good")
        with patch(
            "crank.services.job_pipeline.ingest_jobs",
            side_effect=[RuntimeError("upstream"), JobIngestResult(ingested=1)],
        ), patch("crank.services.job_pipeline._resolve_source_listings", return_value=(0, 0)), patch(
            "crank.services.job_pipeline.agent_runs.record_agent_event"
        ):
            counts = run_job_pipeline(self.run)
        self.assertEqual(counts["sources_failed"], 1)
        self.assertEqual(counts["sources_succeeded"], 1)

    def test_source_failure_does_not_stop_other_sources(self):
        self.source("failed")
        self.source("good")
        results = [JobIngestResult(errors=1), JobIngestResult(ingested=1)]
        with patch("crank.services.job_pipeline.ingest_jobs", side_effect=results), patch(
            "crank.services.job_pipeline._resolve_source_listings", return_value=(0, 0)
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            counts = run_job_pipeline(self.run)
        self.assertEqual(counts["sources_succeeded"], 1)
        self.assertEqual(counts["sources_failed"], 1)
        self.assertEqual(counts["listings_ingested"], 1)

    def test_all_source_failure_raises_with_counts(self):
        self.source("failed")
        with patch(
            "crank.services.job_pipeline.ingest_jobs",
            return_value=JobIngestResult(errors=1),
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            with self.assertRaises(JobPipelineError) as raised:
                run_job_pipeline(self.run)
        self.assertEqual(raised.exception.counts["sources_failed"], 1)

    def test_user_failure_does_not_stop_other_users(self):
        self.preference("alice")
        self.preference("bob")
        with patch(
            "crank.services.job_pipeline._run_user",
            side_effect=[RuntimeError("one user failed"), 2],
        ), patch("crank.services.job_pipeline._active_listings", return_value=[]), patch(
            "crank.services.job_pipeline.agent_runs.record_agent_event"
        ):
            counts = run_job_pipeline(self.run)
        self.assertEqual(counts["users_total"], 2)
        self.assertEqual(counts["users_failed"], 1)
        self.assertEqual(counts["users_succeeded"], 1)
        self.assertEqual(counts["matches_persisted"], 2)

    def test_all_user_failure_raises_with_counts(self):
        self.preference("alice")
        with patch(
            "crank.services.job_pipeline._run_user",
            side_effect=RuntimeError("matching failed"),
        ), patch("crank.services.job_pipeline._active_listings", return_value=[]), patch(
            "crank.services.job_pipeline.agent_runs.record_agent_event"
        ):
            with self.assertRaises(JobPipelineError) as raised:
                run_job_pipeline(self.run)
        self.assertEqual(raised.exception.counts["users_failed"], 1)

    def test_inactive_and_empty_default_preferences_are_skipped(self):
        self.preference("inactive", active=False)
        self.preference("empty", values={})
        with patch("crank.services.job_pipeline._run_user") as matcher, patch(
            "crank.services.job_pipeline.agent_runs.record_agent_event"
        ):
            counts = run_job_pipeline(self.run)
        self.assertEqual(counts["users_total"], 0)
        matcher.assert_not_called()

    def test_deadline_stops_source_and_user_processing(self):
        self.source("late")
        self.preference("alice")
        with patch("crank.services.job_pipeline.ingest_jobs") as ingest, patch(
            "crank.services.job_pipeline._run_user"
        ) as matcher, patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            counts = run_job_pipeline(self.run, deadline_seconds=0)
        self.assertTrue(counts["deadline_reached"])
        ingest.assert_not_called()
        matcher.assert_not_called()

    def test_resolution_and_user_helpers_isolate_and_persist(self):
        listing = SimpleNamespace(
            pk=1,
            status="active",
        )
        source = SimpleNamespace(pk=4)
        resolution = SimpleNamespace(resolved=True)
        with patch("crank.services.job_pipeline.JobListing.all_objects.filter") as rows, patch(
            "crank.services.job_pipeline.resolve_employer", return_value=resolution
        ):
            rows.return_value.order_by.return_value = [listing]
            self.assertEqual(_resolve_source_listings(source, set()), (1, 0))
        unresolved = SimpleNamespace(pk=2, status="closed")
        with patch("crank.services.job_pipeline.JobListing.all_objects.filter") as rows, patch(
            "crank.services.job_pipeline.resolve_employer", return_value=SimpleNamespace(resolved=False)
        ):
            rows.return_value.order_by.return_value = [unresolved]
            self.assertEqual(_resolve_source_listings(source, set()), (0, 1))
        with patch("crank.services.job_pipeline.JobListing.all_objects.filter") as rows, patch(
            "crank.services.job_pipeline.resolve_employer", side_effect=RuntimeError("resolution")
        ):
            rows.return_value.order_by.return_value = [listing]
            self.assertEqual(_resolve_source_listings(source, set()), (0, 1))
        preference = SimpleNamespace(
            preferences={"notes": "x"}, schema_version=1, user=object()
        )
        with patch("crank.services.job_pipeline.project_criteria", return_value=object()), patch(
            "crank.services.job_pipeline.rank_listings"
        ) as rank, patch("crank.services.job_pipeline.persist_matches", return_value=2) as persist:
            self.assertEqual(_run_user(preference, [], {}), 2)
        rank.assert_called_once()
        persist.assert_called_once()

    def test_active_listing_query_is_bounded(self):
        self.assertEqual(_active_listings(1), [])

    def test_replay_emits_same_counts_without_duplicate_pipeline_calls(self):
        self.preference("alice")
        with patch("crank.services.job_pipeline._active_listings", return_value=[]), patch(
            "crank.services.job_pipeline._run_user", return_value=0
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            first = run_job_pipeline(self.run)
            second = run_job_pipeline(self.run)
        self.assertEqual(first, second)


class JobPipelineCommandTests(TestCase):
    def call(self):
        stdout = StringIO()
        stderr = StringIO()
        result = call_command("run_job_pipeline", stdout=stdout, stderr=stderr)
        return result, stdout

    @override_settings(JOB_PIPELINE_ENABLED=False)
    def test_disabled_schedule_does_no_work(self):
        result, stdout = self.call()
        self.assertEqual(result, 0)
        self.assertFalse(AgentRun.objects.exists())
        self.assertIn("disabled", stdout.getvalue())

    @override_settings(AGENT_RUN_ENABLED=True, JOB_PIPELINE_ENABLED=True)
    def test_overlap_is_skipped(self):
        active = agent_runs.claim_run(AgentRun.RunType.JOB_PIPELINE)
        with patch("crank.management.commands.run_job_pipeline.run_job_pipeline") as pipeline:
            result, stdout = self.call()
        self.assertEqual(result, 0)
        pipeline.assert_not_called()
        self.assertTrue(AgentRun.objects.filter(status=AgentRun.Status.SKIPPED).exists())
        active.refresh_from_db()
        self.assertEqual(active.status, AgentRun.Status.RUNNING)
        self.assertIn("skipped", stdout.getvalue())

    @override_settings(AGENT_RUN_ENABLED=True, JOB_PIPELINE_ENABLED=True)
    def test_all_failure_has_nonzero_exit_status_and_counts(self):
        counts = {"sources_total": 1, "sources_failed": 1}
        error = JobPipelineError("all sources failed", counts)
        with patch(
            "crank.management.commands.run_job_pipeline.run_job_pipeline",
            side_effect=error,
        ):
            with self.assertRaises(CommandError):
                self.call()
        run = AgentRun.objects.get(run_type=AgentRun.RunType.JOB_PIPELINE)
        self.assertEqual(run.status, AgentRun.Status.FAILED)
        self.assertEqual(run.counts, counts)

    @override_settings(AGENT_RUN_ENABLED=True, JOB_PIPELINE_ENABLED=True)
    def test_success_event_contains_pipeline_counts(self):
        counts = {"sources_total": 0, "users_total": 0}
        with patch(
            "crank.management.commands.run_job_pipeline.run_job_pipeline",
            return_value=counts,
        ), patch("crank.services.agent_runs.record_agent_event") as event:
            result, _ = self.call()
        self.assertEqual(result, 0)
        self.assertTrue(
            any(
                call.args[1] == "run_succeeded"
                and call.kwargs == {"counts": counts}
                for call in event.call_args_list
            )
        )


def test_job_pipeline_manifest_has_disabled_safe_schedule():
    path = Path(__file__).parents[3] / "deploy" / "cronjob-job-pipeline.yaml"
    document = yaml.safe_load(path.read_text())
    spec = document["spec"]
    container = spec["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert spec["schedule"] == "0 */6 * * *"
    assert spec["suspend"] is True
    assert spec["concurrencyPolicy"] == "Forbid"
    assert container["command"][-1] == "run_job_pipeline"
    assert container["resources"]["limits"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert any(item["name"] == "SECRET_KEY" for item in container["env"])
