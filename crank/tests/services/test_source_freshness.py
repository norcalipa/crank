# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Fair, bounded scheduling and success-only timestamps (issue #468)."""

import importlib
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from django.apps import apps as django_apps
from django.test import TestCase, override_settings
from django.utils import timezone

from crank.agents.jobs.ingest import JobIngestResult
from crank.models import AgentRun, CrawlRun, JobSourceCatalog, Organization, SourceCatalog
from crank.models.company_profile import CompanyFieldEvidence
from crank.models.source import ApprovalState
from crank.services import source_freshness as sf
from crank.services.crawl_scheduler import plan_crawls
from crank.services.job_pipeline import COUNT_KEYS, run_job_pipeline
from crank.services.monitoring import _SAFE_KEYS, event_attributes

NOW = timezone.now().replace(microsecond=0)
POLICY = sf.RefreshPolicy(
    ttl=timedelta(hours=6),
    tolerance=timedelta(minutes=15),
    retry_base=timedelta(minutes=60),
    retry_max=timedelta(hours=24),
)
APPROVED = "approved"


def src(pk, **kw):
    values = dict(
        pk=pk, approval_state=APPROVED, enabled=True, last_crawl_at=None,
        last_attempt_at=None, consecutive_failures=0,
    )
    values.update(kw)
    return SimpleNamespace(**values)


class PolicyTests(TestCase):
    def test_retry_after_is_exponential_and_capped(self):
        self.assertEqual(sf.retry_after(0, POLICY), timedelta(0))
        self.assertEqual(sf.retry_after(1, POLICY), timedelta(hours=1))
        self.assertEqual(sf.retry_after(3, POLICY), timedelta(hours=4))
        self.assertEqual(sf.retry_after(50, POLICY), timedelta(hours=24))

    @override_settings(JOB_REFRESH_TTL_HOURS=2, ORGANIZATION_FRESHNESS_HOURS=5)
    def test_policies_read_settings(self):
        self.assertEqual(sf.job_policy().ttl, timedelta(hours=2))
        self.assertEqual(sf.organization_policy().ttl, timedelta(hours=5))
        self.assertEqual(sf.job_policy().tolerance, timedelta(minutes=15))

    def test_classify(self):
        def c(**kw):
            return sf.classify(src(1, **kw), POLICY, now=NOW, approved_value=APPROVED)

        self.assertEqual(c(enabled=False), "policy")
        self.assertEqual(c(approval_state="pending"), "policy")
        self.assertEqual(c(last_crawl_at=NOW - timedelta(hours=5)), "fresh")
        # inside tolerance: 5h50m old is due again (ttl - 15m = 5h45m)
        self.assertEqual(c(last_crawl_at=NOW - timedelta(hours=5, minutes=50)), "due")
        self.assertEqual(
            c(last_attempt_at=NOW - timedelta(minutes=10), consecutive_failures=1),
            "backoff",
        )
        self.assertEqual(
            c(last_attempt_at=NOW - timedelta(hours=2), consecutive_failures=1), "due"
        )
        self.assertEqual(c(), "due")

    def test_next_eligible_at(self):
        self.assertIsNone(sf.next_eligible_at(src(1), POLICY))
        fresh = src(1, last_crawl_at=NOW)
        self.assertEqual(
            sf.next_eligible_at(fresh, POLICY), NOW + timedelta(hours=5, minutes=45)
        )
        failing = src(1, last_attempt_at=NOW, consecutive_failures=2)
        self.assertEqual(sf.next_eligible_at(failing, POLICY), NOW + timedelta(hours=2))

    def test_select_due_orders_by_attempt_then_success_then_pk(self):
        old = NOW - timedelta(days=2)
        sources = [
            src(1, last_attempt_at=NOW - timedelta(days=1), last_crawl_at=old),
            src(2, last_attempt_at=old, last_crawl_at=old),
            src(3),
            src(4),
            src(5, enabled=False),
            src(6, last_crawl_at=NOW),
            src(7, last_attempt_at=NOW, consecutive_failures=1),
        ]
        sel = sf.select_due(sources, POLICY, now=NOW, approved_value=APPROVED, limit=3)
        self.assertEqual([s.pk for s in sel.selected], [3, 4, 2])
        self.assertEqual(
            sel.counts,
            {"eligible": 6, "skipped_policy": 1, "skipped_fresh": 1,
             "deferred_backoff": 1, "due": 4},
        )
        self.assertEqual(sel.oldest_due_age_hours, sf.MAX_AGE_HOURS)

    def test_select_due_age_and_empty(self):
        sel = sf.select_due(
            [src(1, last_crawl_at=NOW - timedelta(hours=10))],
            POLICY, now=NOW, approved_value=APPROVED, limit=5,
        )
        self.assertEqual(sel.oldest_due_age_hours, 10)
        empty = sf.select_due([], POLICY, now=NOW, approved_value=APPROVED, limit=0)
        self.assertEqual(empty.selected, [])
        self.assertIsNone(empty.oldest_due_age_hours)

    def test_outcome_mapping(self):
        self.assertEqual(sf.organization_outcome(SimpleNamespace(errors=0)), sf.Outcome.SUCCESS)
        self.assertEqual(sf.organization_outcome(SimpleNamespace(errors=2)), sf.Outcome.PARTIAL)
        self.assertEqual(sf.organization_outcome(SimpleNamespace(errors="x")), sf.Outcome.PARTIAL)
        self.assertEqual(sf.job_outcome(JobIngestResult()), sf.Outcome.SUCCESS)
        self.assertEqual(sf.job_outcome(JobIngestResult(errors=1)), sf.Outcome.PARTIAL)
        self.assertEqual(
            sf.job_outcome(JobIngestResult(errors=1, closure_skipped_reason="fetch_error")),
            sf.Outcome.FAILED,
        )
        self.assertEqual(sf.outcome_from_crawl_run("success"), sf.Outcome.SUCCESS)
        self.assertEqual(sf.outcome_from_crawl_run("partial"), sf.Outcome.PARTIAL)
        self.assertEqual(sf.outcome_from_crawl_run("timeout"), sf.Outcome.FAILED)
        self.assertEqual(sf.outcome_from_crawl_run("failure"), sf.Outcome.FAILED)

    def test_record_outcome_increments_and_resets(self):
        job = JobSourceCatalog.objects.create(
            name="j", adapter_key="a", base_url="https://jobs.example.test",
            approval_state=APPROVED, enabled=True,
        )
        sf.record_outcome(JobSourceCatalog, job.pk, sf.Outcome.FAILED, now=NOW)
        sf.record_outcome(JobSourceCatalog, job.pk, sf.Outcome.PARTIAL, now=NOW)
        job.refresh_from_db()
        self.assertEqual(job.consecutive_failures, 2)
        self.assertEqual(job.last_attempt_at, NOW)
        self.assertIsNone(job.last_crawl_at)
        sf.record_outcome(JobSourceCatalog, job.pk, sf.Outcome.SUCCESS, now=NOW)
        job.refresh_from_db()
        self.assertEqual((job.consecutive_failures, job.last_crawl_at), (0, NOW))


def org_sources(n):
    return [
        SourceCatalog.objects.create(
            name=f"s{i}", adapter_key="a", base_url="https://www.google.com",
            approval_state=ApprovalState.APPROVED, enabled=True,
            organization=Organization.objects.create(
                name=f"Org {i}", url=f"https://org{i}.example.test"
            ),
        )
        for i in range(n)
    ]


def ok(source, now):
    return SimpleNamespace(errors=0)


class OrganizationPlannerTests(TestCase):
    def test_budget_limited_runs_dispatch_every_source_once(self):
        sources = org_sources(5)
        seen = []

        def dispatch(source, now):
            seen.append(source.pk)
            return SimpleNamespace(errors=0)

        for i in range(3):
            counts = plan_crawls(
                phase="organization", max_sources=2, now=NOW + timedelta(minutes=i),
                dispatchers={"organization": dispatch},
            )
        self.assertEqual(seen, [s.pk for s in sources])
        self.assertEqual(counts["skipped_fresh"], 5 - 1)
        self.assertEqual(counts["succeeded"], 1)

    def test_failing_first_source_does_not_starve_others(self):
        first, *rest = org_sources(4)

        def dispatch(source, now):
            if source.pk == first.pk:
                raise RuntimeError("boom")
            return SimpleNamespace(errors=0)

        order = []
        for i in range(4):
            with patch("crank.services.crawl_scheduler._dispatch_organization"):
                pass
            counts = plan_crawls(
                phase="organization", max_sources=1, now=NOW + timedelta(hours=2 * i),
                dispatchers={"organization": lambda s, n: (order.append(s.pk), dispatch(s, n))[1]},
            )
        self.assertEqual(order[:4], [first.pk] + [s.pk for s in rest[:3]])
        first.refresh_from_db()
        self.assertIsNone(first.last_crawl_at)
        self.assertEqual(first.consecutive_failures, 1)
        self.assertEqual(first.last_attempt_at, NOW)
        self.assertEqual(counts["succeeded"], 1)

    def test_backoff_and_partial_and_counts(self):
        a, b, c = org_sources(3)
        b.enabled = False
        b.save()
        results = {a.pk: SimpleNamespace(errors=2)}
        counts = plan_crawls(
            phase="organization", max_sources=1, now=NOW,
            dispatchers={"organization": lambda s, n: results[s.pk]},
        )
        a.refresh_from_db()
        self.assertIsNone(a.last_crawl_at)
        self.assertEqual(a.consecutive_failures, 1)
        self.assertEqual(
            (counts["partial"], counts["deferred_budget"], counts["skipped_policy"],
             counts["eligible"], counts["stale"], counts["skipped"], counts["errors"]),
            (1, 1, 1, 2, 2, 2, 2),
        )
        again = plan_crawls(
            phase="organization", max_sources=5, now=NOW + timedelta(minutes=5),
            dispatchers={"organization": ok},
        )
        self.assertEqual(again["deferred_backoff"], 1)
        self.assertEqual(again["scheduled"], 1)

    def test_deadline_defers_without_touching_source(self):
        (a,) = org_sources(1)
        with patch("crank.services.crawl_scheduler.time.monotonic", side_effect=[0.0, 10.0]):
            counts = plan_crawls(
                phase="organization", now=NOW, deadline_seconds=1,
                dispatchers={"organization": ok},
            )
        a.refresh_from_db()
        self.assertIsNone(a.last_attempt_at)
        self.assertEqual((counts["scheduled"], counts["deferred_budget"]), (0, 1))

    def test_failed_dispatch_leaves_evidence_untouched(self):
        (a,) = org_sources(1)
        evidence = CompanyFieldEvidence.objects.create(
            organization=a.organization,
            field_key=CompanyFieldEvidence.FieldKey.LOCATIONS,
            value_text="10", source_url="https://org0.example.test/x",
            observed_at=NOW,
            last_verified_at=NOW - timedelta(days=3),
            last_changed_at=NOW - timedelta(days=9),
        )
        plan_crawls(
            phase="organization", now=NOW,
            dispatchers={"organization": lambda s, n: 1 / 0},
        )
        evidence.refresh_from_db()
        self.assertEqual(evidence.last_verified_at, NOW - timedelta(days=3))
        self.assertEqual(evidence.last_changed_at, NOW - timedelta(days=9))
        self.assertEqual(evidence.value_text, "10")

    def test_event_payload_keys_are_allowlisted(self):
        org_sources(1)
        with patch("crank.services.crawl_scheduler.monitoring.record_event") as event:
            counts = plan_crawls(phase="organization", now=NOW, dispatchers={"organization": ok})
        payload = event.call_args.args[1]
        self.assertEqual(payload, counts)
        self.assertLessEqual(set(payload), _SAFE_KEYS)
        self.assertLessEqual(
            {"eligible", "skipped_policy", "skipped_fresh", "deferred_backoff",
             "deferred_budget", "succeeded", "partial", "failed", "oldest_due_age_hours"},
            set(event_attributes("crawl_planning", payload)),
        )
        self.assertLessEqual(payload["oldest_due_age_hours"], sf.MAX_AGE_HOURS)


def job_sources(n, start=0):
    return [
        JobSourceCatalog.objects.create(
            name=f"j{i}", adapter_key="fake.v1", base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED, enabled=True,
        )
        for i in range(start, start + n)
    ]


class JobPipelineFreshnessTests(TestCase):
    def setUp(self):
        self.run = AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE, status=AgentRun.Status.RUNNING
        )

    def go(self, results, **options):
        from crank.services.job_ingest import JobSourceIngestion

        wrapped = [
            r if isinstance(r, Exception) else JobSourceIngestion(result=r, skipped=r is None, reason="")
            for r in results
        ]
        with patch("crank.services.job_pipeline.ingest_job_source", side_effect=wrapped), patch(
            "crank.services.job_pipeline._resolve_source_listings", return_value=(0, 0)
        ), patch("crank.services.job_pipeline.agent_runs.record_agent_event"), patch(
            "crank.services.job_pipeline.agent_runs.monitoring.record_event"
        ) as event:
            try:
                counts = run_job_pipeline(self.run, **options)
            except Exception as exc:  # JobPipelineError still carries counts
                counts = exc.counts
        return counts, event

    def test_sources_beyond_the_budget_are_ingested_on_later_runs(self):
        sources = job_sources(5)
        ingested = []
        for i in range(3):
            counts, event = self.go(
                [JobIngestResult()] * 2, JOB_PIPELINE_MAX_SOURCES=2,
                now=NOW + timedelta(minutes=i),
            )
            ingested.append(counts["sources_succeeded"])
        self.assertEqual(ingested, [2, 2, 1])
        for s in sources:
            s.refresh_from_db()
            self.assertIsNotNone(s.last_crawl_at)
        self.assertEqual(counts["sources_eligible"], 5)
        payload = event.call_args.args[1]
        self.assertLessEqual({"sources_eligible", "sources_deferred", "oldest_source_age_hours"}, set(payload))
        self.assertLessEqual(set(COUNT_KEYS) - {"deadline_reached", "sources_skipped"}, set(event_attributes("matching_batch", payload)))

    def test_failing_first_source_does_not_starve(self):
        first, second = job_sources(2)
        self.go([RuntimeError("x")], JOB_PIPELINE_MAX_SOURCES=1, now=NOW)
        counts, _ = self.go(
            [JobIngestResult()], JOB_PIPELINE_MAX_SOURCES=1, now=NOW + timedelta(minutes=1)
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.consecutive_failures, first.last_crawl_at), (1, None))
        self.assertEqual(first.last_attempt_at, NOW)
        self.assertIsNotNone(second.last_crawl_at)
        self.assertEqual(counts["sources_succeeded"], 1)

    def test_partial_fetch_error_lock_skip_and_fresh(self):
        a, b, c = job_sources(3)
        self.go(
            [JobIngestResult(errors=1), JobIngestResult(errors=1, closure_skipped_reason="fetch_error"), None],
            now=NOW,
        )
        for s in (a, b):
            s.refresh_from_db()
            self.assertEqual((s.consecutive_failures, s.last_crawl_at), (1, None))
        c.refresh_from_db()
        self.assertEqual((c.last_attempt_at, c.consecutive_failures), (None, 0))
        counts, _ = self.go([JobIngestResult()], now=NOW + timedelta(minutes=30))
        self.assertEqual(counts["sources_succeeded"], 1)  # c only; a and b are in backoff
        self.assertEqual(counts["sources_total"], 1)
        counts, _ = self.go([], now=NOW + timedelta(minutes=31), JOB_PIPELINE_MAX_SOURCES=0)
        self.assertEqual(counts["sources_total"], 0)
        c.refresh_from_db()
        self.assertEqual(c.consecutive_failures, 0)

    def test_success_resets_failures_and_is_fresh_within_ttl(self):
        (a,) = job_sources(1)
        self.go([JobIngestResult(errors=1)], now=NOW)
        self.go([JobIngestResult()], now=NOW + timedelta(hours=2))
        a.refresh_from_db()
        self.assertEqual((a.consecutive_failures, a.last_crawl_at), (0, NOW + timedelta(hours=2)))
        counts, _ = self.go([], now=NOW + timedelta(hours=3))
        self.assertEqual(counts["sources_total"], 0)
        counts, _ = self.go([JobIngestResult()], now=NOW + timedelta(hours=8))
        self.assertEqual(counts["sources_total"], 1)


class ManualCrawlOutcomeTests(TestCase):
    def test_manual_outcomes_record_on_the_source(self):
        from crank.services.crawl_runs import SourceLockHeld, trigger_crawl

        (job,) = job_sources(1)

        def run(result=None, exc=None):
            target = "crank.services.crawl_runs._execute"
            with patch(target, side_effect=exc) if exc else patch(target, return_value=result):
                trigger_crawl(source_key=str(job.pk), source_type="job")
            job.refresh_from_db()

        run(SimpleNamespace(errors=1, ingested=1, updated=0, total=1))
        self.assertEqual((job.consecutive_failures, job.last_crawl_at), (1, None))
        run(exc=RuntimeError("bad"))
        self.assertEqual(job.consecutive_failures, 2)
        run(exc=SourceLockHeld())
        self.assertEqual(job.consecutive_failures, 2)
        run(SimpleNamespace(errors=0, ingested=1, updated=0, total=1))
        self.assertEqual(job.consecutive_failures, 0)
        self.assertIsNotNone(job.last_crawl_at)
        self.assertEqual(CrawlRun.objects.count(), 4)


class InventoryAgeTests(TestCase):
    def test_last_success_uses_source_success_only(self):
        from crank.empty_state import _inventory_facts

        self.assertIsNone(_inventory_facts(now=NOW, active_listings_count=0)["last_success_at"])
        (job,) = job_sources(1)
        agent_run = AgentRun.objects.create(run_type=AgentRun.RunType.CRAWL, status=AgentRun.Status.SUCCEEDED)
        CrawlRun.objects.create(
            source_type="job", source_key="k", job_source=job, agent_run=agent_run,
            started_at=NOW, finished_at=NOW, outcome=CrawlRun.Outcome.PARTIAL,
        )
        self.assertIsNone(_inventory_facts(now=NOW, active_listings_count=0)["last_success_at"])
        sf.record_outcome(JobSourceCatalog, job.pk, sf.Outcome.SUCCESS, now=NOW - timedelta(hours=3))
        facts = _inventory_facts(now=NOW, active_listings_count=1)
        self.assertEqual(facts["age_hours"], 3.0)

    def test_pipeline_success_clears_stale_health(self):
        from crank.services.inventory_health import check_inventory_health

        (job,) = job_sources(1)
        self.assertEqual(check_inventory_health()["stale_sources"], 1)
        sf.record_outcome(JobSourceCatalog, job.pk, sf.Outcome.SUCCESS, now=timezone.now())
        self.assertEqual(check_inventory_health()["stale_sources"], 0)


class MigrationAndManifestTests(TestCase):
    def test_backfill_uses_latest_success_only_and_is_idempotent(self):
        migration = importlib.import_module("crank.migrations.0040_source_refresh_state")
        (job,) = job_sources(1)
        (org,) = org_sources(1)
        keep = job_sources(1, start=10)[0]
        keep.last_crawl_at = NOW
        keep.save(update_fields=["last_crawl_at"])
        agent_run = AgentRun.objects.create(run_type=AgentRun.RunType.CRAWL, status=AgentRun.Status.SUCCEEDED)
        for kind, target, field in (("job", job, "job_source"), ("organization", org, "source")):
            for outcome, hours in (("success", 5), ("partial", 1)):
                CrawlRun.objects.create(
                    source_type=kind, source_key=f"{kind}{hours}", agent_run=agent_run,
                    started_at=NOW - timedelta(hours=hours), finished_at=NOW - timedelta(hours=hours),
                    outcome=outcome, **{field: target},
                )
        migration.backfill_last_crawl_at(django_apps, None)
        migration.backfill_last_crawl_at(django_apps, None)
        for row in (job, org, keep):
            row.refresh_from_db()
        self.assertEqual(job.last_crawl_at, NOW - timedelta(hours=5))
        self.assertEqual(org.last_crawl_at, NOW - timedelta(hours=5))
        self.assertEqual(keep.last_crawl_at, NOW)

    def test_crawl_cron_manifests_stay_suspended(self):
        root = Path(__file__).parents[3]
        for name in ("k8s/crank-crawl-cron.yaml", "deploy/cronjob-job-pipeline.yaml"):
            documents = yaml.safe_load_all((root / name).read_text())
            cron = next(d for d in documents if d and d.get("kind") == "CronJob")
            self.assertIs(cron["spec"]["suspend"], True)
