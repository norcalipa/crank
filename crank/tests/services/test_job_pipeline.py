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
from django.utils import timezone

from crank.agents.jobs.ingest import JobIngestResult
from crank.models import (
    AgentRun,
    JobListing,
    JobSourceCatalog,
    Organization,
    PublicationEvent,
    UserPreference,
)
from crank.services import agent_runs
from crank.services.job_ingest import SKIP_OVERLAP, JobSourceIngestion
from crank.services import publication
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


def ingestion(result=None, *, skipped=False, reason=""):
    """Build a boundary outcome for tests."""
    return JobSourceIngestion(result=result, skipped=skipped, reason=reason)


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

    def _listing(self, source, organization, external_id):
        now = timezone.now()
        return JobListing.all_objects.create(
            source=source,
            external_id=external_id,
            canonical_url=f"https://jobs.example.test/{external_id}",
            employer_name=organization.name,
            title="Engineer",
            first_seen_at=now,
            last_seen_at=now,
            organization=organization,
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
        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            return_value=ingestion(result),
        ), patch(
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
            "crank.services.job_pipeline.ingest_job_source",
            side_effect=[RuntimeError("upstream"), ingestion(JobIngestResult(ingested=1))],
        ), patch("crank.services.job_pipeline._resolve_source_listings", return_value=(0, 0)), patch(
            "crank.services.job_pipeline.agent_runs.record_agent_event"
        ):
            counts = run_job_pipeline(self.run)
        self.assertEqual(counts["sources_failed"], 1)
        self.assertEqual(counts["sources_succeeded"], 1)

    def test_source_failure_does_not_stop_other_sources(self):
        self.source("failed")
        self.source("good")
        results = [
            ingestion(JobIngestResult(errors=1)),
            ingestion(JobIngestResult(ingested=1)),
        ]
        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            side_effect=results,
        ), patch(
            "crank.services.job_pipeline._resolve_source_listings", return_value=(0, 0)
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            counts = run_job_pipeline(self.run)
        self.assertEqual(counts["sources_succeeded"], 1)
        self.assertEqual(counts["sources_failed"], 1)
        self.assertEqual(counts["listings_ingested"], 1)

    def test_locked_source_is_skipped_not_failed(self):
        # A source whose per-source lock is held by another ingestion path is
        # skipped with a recorded reason, never double-fetched (issue #462).
        self.source("contended")
        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            return_value=ingestion(None, skipped=True, reason=SKIP_OVERLAP),
        ), patch(
            "crank.services.job_pipeline._resolve_source_listings"
        ) as resolve, patch(
            "crank.services.job_pipeline.agent_runs.record_agent_event"
        ):
            counts = run_job_pipeline(self.run)
        self.assertEqual(counts["sources_skipped"], 1)
        self.assertEqual(counts["sources_failed"], 0)
        self.assertEqual(counts["sources_succeeded"], 0)
        resolve.assert_not_called()

    def test_all_sources_skipped_does_not_raise_pipeline_error(self):
        # Contention skips are transient, not failures: an all-skipped run
        # must not fail the pipeline with "all sources failed".
        self.source("contended")
        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            return_value=ingestion(None, skipped=True, reason=SKIP_OVERLAP),
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            counts = run_job_pipeline(self.run)
        self.assertEqual(counts["sources_skipped"], 1)

    def test_all_source_failure_raises_with_counts(self):
        self.source("failed")
        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            return_value=ingestion(JobIngestResult(errors=1)),
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            with self.assertRaises(JobPipelineError) as raised:
                run_job_pipeline(self.run)
        self.assertEqual(raised.exception.counts["sources_failed"], 1)

    @override_settings(JOB_PIPELINE_DEADLINE_SECONDS=300)
    def test_successful_source_records_one_bounded_publication_event(self):
        source = self.source("good")
        employer = Organization.objects.create(
            name="Employer One", url="https://employer.one"
        )
        other = Organization.objects.create(
            name="Employer Two", url="https://employer.two"
        )
        self._listing(source, employer, "ext-1")
        self._listing(source, employer, "ext-1-again")  # same org: deduped
        self._listing(source, other, "ext-2")

        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            return_value=ingestion(JobIngestResult(ingested=3, updated=1)),
        ), patch(
            "crank.services.job_pipeline._resolve_source_listings",
            return_value=(3, 0),
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            counts = run_job_pipeline(self.run)

        self.assertEqual(counts["sources_succeeded"], 1)
        events = PublicationEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.target_type, PublicationEvent.TargetType.LISTING)
        self.assertEqual(event.target_id, source.id)
        self.assertEqual(event.event_kind, PublicationEvent.EventKind.INGESTED)
        self.assertEqual(event.payload["source_key"], "fake.v1")
        self.assertEqual(event.payload["ingested"], 3)
        self.assertEqual(event.payload["updated"], 1)
        self.assertEqual(event.payload["resolved"], 3)
        self.assertEqual(
            sorted(event.payload["organization_ids"]),
            sorted([employer.id, other.id]),
        )
        self.assertEqual(event.payload["chunk_count"], 1)
        self.assertEqual(event.payload["chunk_index"], 0)

    @override_settings(JOB_PIPELINE_DEADLINE_SECONDS=300)
    def test_failed_source_records_no_publication_event(self):
        self.source("failed")

        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            return_value=ingestion(JobIngestResult(errors=1)),
        ), patch(
            "crank.services.job_pipeline._resolve_source_listings",
            return_value=(0, 0),
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            # A population that completely fails raises; the assertion is
            # that the failure still records no publication events.
            with self.assertRaises(JobPipelineError):
                run_job_pipeline(self.run)

        self.assertEqual(PublicationEvent.objects.count(), 0)

    @override_settings(JOB_PIPELINE_DEADLINE_SECONDS=300)
    def test_source_publication_records_all_organizations_in_bounded_chunks(self):
        """No organization id is ever dropped: a source mapped to more
        organizations than one payload chunk holds emits multiple bounded
        chunk events whose union covers every employer (review finding:
        truncated listing targets)."""
        source = self.source("good")
        organizations = [
            Organization.objects.create(
                name=f"Employer {index}", url=f"https://employer{index}.test"
            )
            for index in range(publication.MAX_PAYLOAD_ORGANIZATION_IDS + 1)
        ]
        for index, organization in enumerate(organizations):
            self._listing(source, organization, f"ext-{index}")

        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            return_value=ingestion(JobIngestResult(
                ingested=publication.MAX_PAYLOAD_ORGANIZATION_IDS + 1
            )),
        ), patch(
            "crank.services.job_pipeline._resolve_source_listings",
            return_value=(0, 0),
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            run_job_pipeline(self.run)

        events = list(PublicationEvent.objects.order_by("id"))
        self.assertEqual(len(events), 2)
        chunked_ids = []
        for chunk_index, event in enumerate(events):
            self.assertLessEqual(
                len(event.payload["organization_ids"]),
                publication.MAX_PAYLOAD_ORGANIZATION_IDS,
            )
            self.assertEqual(event.payload["chunk_index"], chunk_index)
            self.assertEqual(event.payload["chunk_count"], 2)
            chunked_ids.extend(event.payload["organization_ids"])
        self.assertEqual(
            sorted(chunked_ids),
            sorted(organization.id for organization in organizations),
        )

    @override_settings(JOB_PIPELINE_DEADLINE_SECONDS=300)
    def test_reassigned_listing_publishes_former_organization_too(self):
        """A listing that moves from organization A to B during resolution
        makes A affected as well: the publication event must carry the
        union of the pre-stage and post-stage organization ids so the former
        organization's caches are invalidated too (review finding: former
        org omitted from invalidation events)."""
        source = self.source("good")
        former = Organization.objects.create(
            name="Former Employer", url="https://former.test"
        )
        current = Organization.objects.create(
            name="Current Employer", url="https://current.test"
        )
        self._listing(source, former, "ext-1")
        self._listing(source, current, "ext-2")

        def reassign(*args, **kwargs):
            # Simulate resolution moving ext-1 from the former org to the
            # current one, as an operator-alias change can.
            listing = JobListing.all_objects.get(source=source, external_id="ext-1")
            listing.organization = current
            listing.save(update_fields=["organization"])
            return (1, 0)

        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            return_value=ingestion(JobIngestResult(ingested=1)),
        ), patch(
            "crank.services.job_pipeline._resolve_source_listings",
            side_effect=reassign,
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            run_job_pipeline(self.run)

        events = PublicationEvent.objects.all()
        self.assertEqual(events.count(), 1)
        self.assertEqual(
            sorted(events.get().payload["organization_ids"]),
            sorted([former.id, current.id]),
        )

    @override_settings(JOB_PIPELINE_DEADLINE_SECONDS=300)
    def test_unresolved_listing_publishes_former_organization_too(self):
        """A listing that becomes unresolved leaves its former organization
        affected even though no listing maps to it afterward: the pre-stage
        snapshot must keep it in the publication event (review finding:
        former org omitted from invalidation events)."""
        source = self.source("good")
        former = Organization.objects.create(
            name="Former Employer", url="https://former.test"
        )
        self._listing(source, former, "ext-1")

        def unresolved(*args, **kwargs):
            listing = JobListing.all_objects.get(source=source, external_id="ext-1")
            listing.organization = None
            listing.save(update_fields=["organization"])
            return (0, 1)

        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            return_value=ingestion(JobIngestResult(ingested=1)),
        ), patch(
            "crank.services.job_pipeline._resolve_source_listings",
            side_effect=unresolved,
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            run_job_pipeline(self.run)

        events = PublicationEvent.objects.all()
        self.assertEqual(events.count(), 1)
        self.assertEqual(
            events.get().payload["organization_ids"],
            [former.id],
        )

    @override_settings(JOB_PIPELINE_DEADLINE_SECONDS=300)
    def test_source_event_commits_with_accepted_writes(self):
        """The listing event commits in the same transaction as the
        source's accepted writes (review finding: non-transactional listing
        events)."""
        source = self.source("good")
        employer = Organization.objects.create(
            name="Employer Atomic", url="https://employer-atomic.test"
        )

        def accepted_ingest(source, query, adapter=None):
            self._listing(source, employer, "ext-atomic")
            return ingestion(JobIngestResult(ingested=1))

        with patch(
            "crank.services.job_pipeline.ingest_job_source", side_effect=accepted_ingest
        ), patch(
            "crank.services.job_pipeline._resolve_source_listings",
            return_value=(0, 0),
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            counts = run_job_pipeline(self.run)

        self.assertEqual(counts["sources_succeeded"], 1)
        self.assertEqual(
            JobListing.all_objects.filter(source=source).count(), 1
        )
        event = PublicationEvent.objects.get()
        self.assertEqual(event.target_id, source.id)

    @override_settings(JOB_PIPELINE_DEADLINE_SECONDS=300)
    def test_source_event_rolls_back_with_accepted_writes(self):
        """An outbox insert failure rolls the source's accepted writes back:
        committed data can never be left without its event (review finding:
        non-transactional listing events)."""
        source = self.source("good")
        employer = Organization.objects.create(
            name="Employer Rollback", url="https://employer-rollback.test"
        )

        def accepted_ingest(source, query, adapter=None):
            self._listing(source, employer, "ext-rollback")
            return ingestion(JobIngestResult(ingested=1))

        with patch(
            "crank.services.job_pipeline.ingest_job_source", side_effect=accepted_ingest
        ), patch(
            "crank.services.job_pipeline._resolve_source_listings",
            return_value=(0, 0),
        ), patch(
            "crank.services.publication.record_event",
            side_effect=RuntimeError("outbox insert failed"),
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            # The failed source rolls back and the (single-source) population
            # raises; the assertions are on the durable state after.
            with self.assertRaises(JobPipelineError):
                run_job_pipeline(self.run)

        self.assertEqual(JobListing.all_objects.filter(source=source).count(), 0)
        self.assertEqual(PublicationEvent.objects.count(), 0)

    @override_settings(JOB_PIPELINE_DEADLINE_SECONDS=300)
    def test_partial_failure_source_still_records_event_for_accepted_writes(self):
        """``result.errors > 0`` no longer skips publication: rows the
        source accepted before the failing row must still be published
        (review finding: non-transactional listing events)."""
        source = self.source("good")
        employer = Organization.objects.create(
            name="Employer Partial", url="https://employer-partial.test"
        )
        self._listing(source, employer, "ext-partial")

        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            return_value=ingestion(JobIngestResult(ingested=2, errors=1)),
        ), patch(
            "crank.services.job_pipeline._resolve_source_listings",
            return_value=(1, 0),
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            # A population whose only source failed raises, but the rows it
            # accepted were committed with their event before the raise.
            with self.assertRaises(JobPipelineError) as raised:
                run_job_pipeline(self.run)

        self.assertEqual(raised.exception.counts["sources_failed"], 1)
        event = PublicationEvent.objects.get()
        self.assertEqual(event.payload["ingested"], 2)
        self.assertEqual(event.payload["resolved"], 1)
        self.assertEqual(event.payload["organization_ids"], [employer.id])

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
        with patch("crank.services.job_pipeline.ingest_job_source") as ingest, patch(
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
    # SECRET_KEY is injected via envFrom from db-connect-credentials (the
    # Secret that actually exists in the cluster). There must be no explicit
    # env block pointing at a nonexistent "crank-secrets" Secret.
    assert "env" not in container
    secret_refs = [
        item["secretRef"]["name"]
        for item in container.get("envFrom", [])
        if "secretRef" in item
    ]
    assert "db-connect-credentials" in secret_refs
    assert "crank-secrets" not in secret_refs


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class RetentionSweepTests(TestCase):
    """Issue #469 AC-12/13: bounded retention sweep and cascade behavior."""

    def setUp(self):
        from crank.models import AgentRun

        self.run = AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE,
            status=AgentRun.Status.RUNNING,
        )

    def source(self, name="sweep", **metadata):
        return JobSourceCatalog.objects.create(
            name=name,
            adapter_key="fake.v1",
            base_url="https://jobs.example.test",
            enabled=True,
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            catalog_metadata=metadata or None,
        )

    def _listing(self, source, external_id, *, status=JobListing.Status.ACTIVE, seen_days=0):
        from datetime import timedelta

        now = timezone.now()
        return JobListing.all_objects.create(
            source=source,
            external_id=external_id,
            canonical_url=f"https://jobs.example.test/{external_id}",
            employer_name="Sweep Co",
            title="Engineer",
            first_seen_at=now - timedelta(days=seen_days + 1),
            last_seen_at=now - timedelta(days=seen_days),
            status=status,
        )

    def test_retention_days_fallbacks(self):
        from crank.services.job_pipeline import (
            DEFAULT_DELETION_DAYS,
            DEFAULT_EXPIRY_DAYS,
            _retention_days,
        )

        plain = self.source("plain")
        self.assertEqual(_retention_days(plain), (DEFAULT_EXPIRY_DAYS, DEFAULT_DELETION_DAYS))
        tuned = self.source("tuned", expiry_days=10, deletion_days=40)
        self.assertEqual(_retention_days(tuned), (10, 40))
        bad_order = self.source("bad-order", expiry_days=50, deletion_days=20)
        self.assertEqual(_retention_days(bad_order), (DEFAULT_EXPIRY_DAYS, DEFAULT_DELETION_DAYS))
        bad_type = self.source("bad-type", expiry_days="soon", deletion_days=None)
        self.assertEqual(_retention_days(bad_type), (DEFAULT_EXPIRY_DAYS, DEFAULT_DELETION_DAYS))

    def test_sweep_expires_stale_and_deletes_terminal(self):
        from crank.services.job_pipeline import _retention_sweep

        source = self.source("sweep-src", expiry_days=10, deletion_days=40)
        fresh = self._listing(source, "fresh", seen_days=1)
        stale = self._listing(source, "stale", seen_days=30)
        old_terminal = self._listing(
            source, "old-terminal", status=JobListing.Status.CLOSED, seen_days=50
        )
        recent_terminal = self._listing(
            source, "recent-terminal", status=JobListing.Status.EXPIRED, seen_days=20
        )
        expired, deleted = _retention_sweep(source)
        self.assertEqual((expired, deleted), (1, 1))
        fresh.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(fresh.status, JobListing.Status.ACTIVE)
        self.assertEqual(stale.status, JobListing.Status.EXPIRED)
        self.assertFalse(JobListing.all_objects.filter(pk=old_terminal.pk).exists())
        recent_terminal.refresh_from_db()
        self.assertEqual(recent_terminal.status, JobListing.Status.EXPIRED)
        # Resumable: a second run is a no-op for these rows.
        self.assertEqual(_retention_sweep(source), (0, 0))

    def test_sweep_is_bounded_and_never_deletes_active(self):
        from crank.services.job_pipeline import _retention_sweep

        source = self.source("bounded", expiry_days=10, deletion_days=40)
        for index in range(5):
            self._listing(source, f"stale-{index}", seen_days=30)
            self._listing(
                source, f"term-{index}", status=JobListing.Status.CLOSED, seen_days=50
            )
        expired, deleted = _retention_sweep(source, limit=2)
        self.assertEqual((expired, deleted), (2, 2))
        self.assertEqual(
            JobListing.all_objects.filter(
                source=source, status=JobListing.Status.ACTIVE
            ).count(),
            3,
        )
        expired, deleted = _retention_sweep(source, limit=10)
        self.assertEqual((expired, deleted), (3, 3))

    def test_deletion_cascades_to_match_and_unresolved(self):
        from crank.models.job_match import JobMatch
        from crank.models.employer import UnresolvedEmployer
        from crank.services.job_pipeline import _retention_sweep

        source = self.source("cascade", expiry_days=10, deletion_days=40)
        listing = self._listing(
            source, "cascade-1", status=JobListing.Status.CLOSED, seen_days=50
        )
        user = User.objects.create_user("cascade-user")
        JobMatch.objects.create(
            user=user,
            listing=listing,
            preference_version=3,
            ranker_version="v1",
            score=0.5,
            first_matched_at=timezone.now(),
            last_matched_at=timezone.now(),
        )
        UnresolvedEmployer.objects.create(
            listing=listing,
            employer_name="Cascade Co",
            reason=UnresolvedEmployer.Reason.NO_MATCH,
        )
        expired, deleted = _retention_sweep(source)
        self.assertEqual(deleted, 1)
        self.assertEqual(JobMatch.objects.filter(listing_id=listing.pk).count(), 0)
        self.assertEqual(
            UnresolvedEmployer.objects.filter(listing_id=listing.pk).count(), 0
        )

    def test_pipeline_threads_closure_and_sweep_counts(self):
        source = self.source("counts", expiry_days=10, deletion_days=40)
        stale = self._listing(source, "stale", seen_days=30)
        result = JobIngestResult(ingested=1, absent_closed=2)
        with patch(
            "crank.services.job_pipeline.ingest_job_source",
            return_value=ingestion(result),
        ), patch(
            "crank.services.job_pipeline._resolve_source_listings", return_value=(0, 0)
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"):
            counts = run_job_pipeline(self.run)
        self.assertEqual(counts["listings_ingested"], 1)
        self.assertEqual(counts["listings_closed"], 2)
        self.assertEqual(counts["listings_expired"], 1)
        stale.refresh_from_db()
        self.assertEqual(stale.status, JobListing.Status.EXPIRED)

    def test_seen_and_dismissed_survive_close_and_reactivation(self):
        """AC-13: recomputed matches preserve user seen/dismissed state."""
        from crank.models.job_match import JobMatch
        from crank.agents.jobs.match_persist import persist_matches
        from crank.agents.jobs.matching import JobCriteria
        from crank.agents.jobs.ranking_config import DEFAULT_CONFIG

        source = self.source("seen")
        listing = self._listing(source, "seen-1")
        organization = Organization.objects.create(name="Seen Co", public=True)
        listing.organization = organization
        listing.save(update_fields=["organization"])
        user = User.objects.create_user("seen-user")
        criteria = JobCriteria(criteria_version=3)
        self.assertEqual(
            persist_matches(user, [listing], criteria, DEFAULT_CONFIG), 1
        )
        match = JobMatch.objects.get(user=user, listing=listing)
        match.seen_at = timezone.now()
        match.dismissed = True
        match.save(update_fields=["seen_at", "dismissed"])
        # Close the listing, then re-observe it active and recompute.
        listing.status = JobListing.Status.CLOSED
        listing.save(update_fields=["status"])
        listing.status = JobListing.Status.ACTIVE
        listing.save(update_fields=["status"])
        persist_matches(user, [listing], criteria, DEFAULT_CONFIG)
        match.refresh_from_db()
        self.assertIsNotNone(match.seen_at)
        self.assertTrue(match.dismissed)
