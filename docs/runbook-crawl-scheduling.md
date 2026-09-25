<!-- Copyright (c) 2024 Isaac Adams
Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

Owner: maintainer (crank.fyi)
Last reviewed: 2026-09-14
Version/change process: update this runbook with every scheduling or rollout-policy change.

# Crawl scheduling runbook

The company-profile freshness planner is disabled by default. It only dispatches
rows that are approved, enabled, and older than their configured freshness TTL.
The planner records aggregate `scheduled`, `stale`, `skipped`, and `errors`
counters in the `crawl_planning` telemetry event; it never includes organization
names, URLs, provider payloads, or credentials.

## Frequency, TTL, stale threshold and evidence age (issue #468)

Four different things are easy to conflate. They are configured separately
and mean different things:

| Phase | Dispatch frequency (CronJob) | Refresh TTL (planner) | Stale threshold (health, user copy) | Published evidence age |
|---|---|---|---|---|
| Organization profiles | `0 */6 * * *` in `k8s/crank-crawl-cron.yaml` | `ORGANIZATION_FRESHNESS_HOURS` (168) | `ORGANIZATION_FRESHNESS_HOURS` | field `last_verified_at` on `CompanyFieldEvidence` |
| Job sources | `0 */6 * * *` in `deploy/cronjob-job-pipeline.yaml` | `JOB_REFRESH_TTL_HOURS` (6) | `JOB_FRESHNESS_HOURS` (24) | source `last_crawl_at` (last successful fetch) |

- **Dispatch frequency** only decides how often a planner starts. A source
  that is still inside its TTL is skipped (`skipped_fresh`).
- **Refresh TTL** is `now - last_crawl_at < TTL - CRAWL_SCHEDULE_TOLERANCE_MINUTES`
  (default tolerance 15 minutes, absorbing CronJob start jitter).
- **Stale threshold** drives `inventory_health` and the user-facing
  "inventory last refreshed" copy. It is deliberately larger than the job
  refresh TTL.
- **Evidence age** never moves on a failed check.

Selection is fair and bounded. Due sources are ordered by
`last_attempt_at` (never-attempted first), then `last_crawl_at`, then `pk`,
and the first `CRAWL_MAX_SOURCES` / `JOB_PIPELINE_MAX_SOURCES` are dispatched.
Every attempt sets `last_attempt_at`, so a permanently failing source moves to
the back and cannot starve the others. `last_crawl_at` advances **only** on a
fully successful attempt; partial and failed attempts increment
`consecutive_failures` and defer the next attempt by
`min(SOURCE_RETRY_BACKOFF_MINUTES * 2^(failures-1), SOURCE_RETRY_BACKOFF_MAX_HOURS)`.
Sources not reached because of the source budget or deadline are untouched and
keep their priority (`deferred_budget`).

Counters (`crawl_planning` and the pipeline's `matching_batch` event):
`eligible`, `stale` (due), `scheduled`, `skipped_policy` (pending, blocked or
disabled), `skipped_fresh`, `deferred_backoff`, `deferred_budget`, `succeeded`,
`partial`, `failed`, `oldest_due_age_hours` (capped at 8760; a never-crawled
source counts as the cap). Job-pipeline equivalents: `sources_eligible`,
`sources_deferred`, `oldest_source_age_hours`. `manage.py crawl_status` shows
TTL, last success, last attempt, failures and next eligible time for both
phases. Both CronJobs stay `suspend: true`.

## Single ingestion owner (issue #462)

**Job-source ingestion has one owner: the job pipeline**
(`manage.py run_job_pipeline`, dispatched by
`deploy/cronjob-job-pipeline.yaml`). The scheduler no longer fetches job
sources:

- The `crank-crawl-jobs` CronJob was removed from `k8s/crank-crawl-cron.yaml`.
- `schedule_crawls --phase jobs` is an accepted, documented **no-op**: it
  counts present job sources into `jobs_total`/`skipped`, prints an
  explanatory message, and dispatches nothing. Operator scripts that still
  pass `--phase jobs` keep working.
- `PHASE_ALL` now plans organization profiles only.

Manual (`manage.py trigger_crawl --source-type job`) and recurring
(`run_job_pipeline`) invocations share one idempotent service boundary
(`crank/services/job_ingest.py::ingest_job_source`): it enforces the
approved+enabled policy, takes a **per-source advisory lock**
(`job_source:{pk}`, MySQL `GET_LOCK`; a no-op on SQLite/PostgreSQL where the
partial unique constraints apply), and calls `ingest_jobs` exactly once per
lock holder. A second path that cannot take the lock skips the source with a
recorded, sanitized reason (`reason_code=overlap_lock`) instead of fetching or
publishing it twice.

### Two-process MySQL drill (run before activating the crons)

The advisory-lock guard must be validated under separate processes, not just
SQLite partial uniqueness. On a MySQL-connected staging environment:

1. In terminal A, start a bounded pipeline that ingests the target source:
   `python manage.py run_job_pipeline` (with `AGENT_RUN_ENABLED=true` and
   `JOB_PIPELINE_ENABLED=true`). While it is mid-run, hold the source lock by
   pausing the process (or add a temporary sleep) inside ingestion.
2. In terminal B, run
   `python manage.py trigger_crawl --source-key <key> --source-type job --confirm`.
   Expected: terminal B records a `CrawlRun` with outcome `failure` and the
   sanitized summary `Skipped: another ingestion path holds this source's
   lock (reason=overlap_lock).` — no duplicate fetch, no second inventory
   write for listings already upserted by the pipeline.
3. Crash/restart drill: kill the consumer (terminal A) mid-`ingest_jobs`,
   replay the run, and verify no duplicate inventory rows (upsert identity)
   and no silently abandoned work — an unconsumed queued (`pending`) run is
   finalized `failed` with a `Queued run reclaimed ...` summary once it is
   older than `AGENT_RUN_STALE_AFTER_SECONDS` (default 3600s).

## Queued runs are consumed (issue #462)

Dashboard queue actions (`Queue Retrieval`, `Queue Job Pipeline`, `Retry
Failed`) create `pending` `AgentRun` rows. The deployed consumer
(`run_job_pipeline` CronJob tick, or any `AgentRunCommand` invocation) **adopts**
the queued row via `claim_run` — `pending → running`, correlation id
preserved — instead of skipping. A queued row past
`AGENT_RUN_STALE_AFTER_SECONDS` with no consumer is finalized `failed` with an
actionable sanitized summary; it never blocks the slot indefinitely.

Surfacing: the Job Retrieval Operations dashboard reports the queued-run count
and the age of the oldest queued run ("Queued runs awaiting consumer") and the
readiness gate treats a queued row as an active/overlapping run.

## Staging enablement

1. Review the approved/enabled `SourceCatalog` and `JobSourceCatalog` rows and
   provision provider credentials through the existing Kubernetes Secret. Do
   not put credentials in a ConfigMap or repository file.
2. Set `AGENT_RUN_ENABLED=true` and `CRAWL_CRON_ENABLED=true` in
   `crank-agent-config` (plus `JOB_PIPELINE_ENABLED=true` for job ingestion).
   Start with the default `168` hours for organization profiles, then adjust
   `ORGANIZATION_FRESHNESS_HOURS` if the source terms and provider budget
   support a tighter target.
3. Apply `k8s/crank-crawl-cron.yaml` with the CronJob still suspended. Run a
   one-off bounded smoke test first:

   ```sh
   kubectl -n crank create job --from=cronjob/crank-crawl-organizations crawl-smoke-$(date +%s)
   kubectl -n crank logs -l job-name=<smoke-job-name> --all-containers
   ```

   For job sources, smoke-test the pipeline one-off:

   ```sh
   kubectl -n crank create job --from=cronjob/crank-job-pipeline pipeline-smoke-$(date +%s)
   kubectl -n crank logs -l job-name=<pipeline-smoke-job-name> --all-containers
   ```

4. Inspect the command's aggregate counters and source timestamps. Unsuspend
   only the phase that has passed the smoke test:

   ```sh
   kubectl -n crank patch cronjob crank-crawl-organizations -p '{"spec":{"suspend":false}}'
   kubectl -n crank patch cronjob crank-job-pipeline -p '{"spec":{"suspend":false}}'
   ```

## Production override and rollback

The checked-in organization schedule is `0 */6 * * *`. Override
`ORGANIZATION_CRAWL_CRON` in deployment configuration, then update the CronJob
`spec.schedule` explicitly; Kubernetes does not interpolate Django environment
variables into a CronJob schedule. Keep `concurrencyPolicy: Forbid`, the
database-backed `crawl_schedule` singleton guard, source limits, and deadline
guardrails. (The former `JOB_CRAWL_CRON` override is obsolete: job freshness is
the pipeline's `0 */6 * * *` schedule.)

To pause without deleting resources, set the CronJob's `spec.suspend=true`
and/or set `CRAWL_CRON_ENABLED=false` (the scheduler command exits without
claiming work; `JOB_PIPELINE_ENABLED=false` pauses job ingestion the same way).
If a provider is failing, pause that phase, leave its source timestamp stale
for a bounded retry after remediation, and inspect `crawl_planning` telemetry
before resuming. A manual bounded organization dispatch is available with:

```sh
python manage.py schedule_crawls --phase organization --max-sources 1 --deadline-seconds 60
```

For a bounded manual job dispatch, use the pipeline command directly (it is
bounded by `JOB_PIPELINE_MAX_SOURCES` and `JOB_PIPELINE_DEADLINE_SECONDS`) or
target one source with `manage.py trigger_crawl --source-key <key>
--source-type job --confirm`. Do not use `schedule_crawls --phase jobs`; that
phase is a no-op.

### Removing the removed CronJob from live clusters

On environments where `crank-crawl-jobs` had already been unsuspended before
this change, delete it once (the manifest no longer contains it):

```sh
kubectl -n crank patch cronjob crank-crawl-jobs -p '{"spec":{"suspend":true}}'
kubectl -n crank delete cronjob crank-crawl-jobs
```

Rollback = revert the manifests; the code paths are safe with either manifest
state (`--phase jobs` remains an accepted no-op either way).
