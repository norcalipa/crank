<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

Owner: maintainer (crank.fyi)
Last reviewed: 2026-10-09
Version/change process: update this runbook with every initial-crawl or seeding-policy change.

# Runbook: initial job inventory crawl

This runbook takes production from zero `JobListing` rows to a populated
inventory. It assumes the Firecrawl adapter (#365), profile crawler (#366),
scheduler (#367), and admin trigger (#368) are already merged and deployed.

## Status: durable on the cluster the deploy workflows manage

Credentials, flags and the `crank-job-pipeline` CronJob are all durable
across deploys ([#555](https://github.com/norcalipa/crank/issues/555)):

- **Credentials survive a deploy.** `crank-capability-secrets` is created
  with empty values only when it is absent, and never overwritten. Populate
  it with the procedure in `docs/capability-config-contract.md` ("Populate or
  rotate a credential"). Never put a credential in a ConfigMap or in the
  repository.
- **Flags are changed by commits** to `k8s/crank-agent-config.yml`, which
  every deploy re-applies (step 4).
- **Deploys create and update `crank-job-pipeline`** from
  `deploy/cronjob-job-pipeline.yaml`, suspended, with the image tag of that
  deploy and `crank-capability-secrets` in `envFrom` (step 7).

Every deploy waits for the web rollout of the pods it replaces. A manifest-only
enablement usually replaces none, so a green run does not show that the phase
works. This phase's readiness check covers only `AGENT_RUN_ENABLED`
(`_job_pipeline_status` in `crank/capability.py`), so a blank `FIRECRAWL_API_KEY`
or `USAJOBS_AUTH_KEY` never fails readiness, even after a restart: the
source returns no data until the key is in the Secret. Populate the keys
first (step 3) and verify with the smoke job.

## Prerequisites

- Kubernetes access to the `crank` namespace
- Source credentials are read from the `crank-capability-secrets` Kubernetes
  Secret (keys `FIRECRAWL_API_KEY`, `USAJOBS_AUTH_KEY`,
  `USAJOBS_USER_AGENT_EMAIL`); see step 3 to populate them
- `AGENT_RUN_ENABLED`, `JOB_PIPELINE_ENABLED` and `CRAWL_CRON_ENABLED`
  currently `false` (the checked-in values)
- CronJobs `crank-crawl-organizations` and `crank-job-pipeline` exist,
  suspended (every deploy applies them)

## Default budget guardrails

| Setting | Default | Purpose |
| --- | --- | --- |
| `FIRECRAWL_MAX_PAGES` | 10 | Max pages per Firecrawl crawl |
| `FIRECRAWL_MAX_LISTINGS` | 100 | Max listings per crawl |
| `FIRECRAWL_CREDIT_BUDGET` | 10 | Firecrawl credits per crawl |
| `JOB_PIPELINE_MAX_SOURCES` | 10 | Source ingest attempts per `run_job_pipeline` run (lock-skipped sources do not count) |
| `JOB_PIPELINE_MAX_LISTINGS_PER_USER` | 500 | Listings fetched per source and ranked per user |
| `JOB_PIPELINE_DEADLINE_SECONDS` | 300 | Wall-clock budget per run |
| `JOB_REFRESH_TTL_HOURS` | 6 | A successful fetch keeps a source out of the next plan for this long |

For the first production crawl, keep the defaults. One `run_job_pipeline` run
makes at most `JOB_PIPELINE_MAX_SOURCES` ingest attempts and fetches at most
`JOB_PIPELINE_MAX_SOURCES × JOB_PIPELINE_MAX_LISTINGS_PER_USER` listings
(10 × 500 = 5,000 listings), and Firecrawl spend is bounded by
`JOB_PIPELINE_MAX_SOURCES × FIRECRAWL_CREDIT_BUDGET` (10 × 10 = 100 credits).
Lower `JOB_PIPELINE_MAX_LISTINGS_PER_USER` and `FIRECRAWL_CREDIT_BUDGET` for a
cheaper smoke test. This procedure does not go through the `CRAWL_MAX_*`
settings: `schedule_crawls` (organization dispatch) reads `CRAWL_MAX_SOURCES`
and `CRAWL_DEADLINE_SECONDS`; the manual job-crawl path (`trigger_crawl`)
reads `CRAWL_MAX_PAGES` and `CRAWL_MAX_LISTINGS`; the organization crawler
uses the `FIRECRAWL_*` limits above.

## Step 1: seed job sources

```sh
# Dry-run first to inspect what will be created
python manage.py seed_job_sources --dry-run

# Seed for real
python manage.py seed_job_sources
```

This creates `JobSourceCatalog` rows for the curated initial sources. Only
domains on the code-owned `APPROVED_JOB_SOURCE_DOMAINS` allowlist are seeded.
Re-running is safe: it upserts existing rows without duplicating.

## Step 2: verify seeding

```sh
python manage.py crawl_status
```

You should see each source with `approved` state and `yes` enabled, zero
listings, and `never` last crawl.

## Step 3: source credentials

The application reads `FIRECRAWL_API_KEY`, `USAJOBS_AUTH_KEY` and
`USAJOBS_USER_AGENT_EMAIL` from the `crank-capability-secrets` Secret, which
is the last entry in `envFrom` in `k8s/crank.yml` and the `k8s/` CronJobs.

- **Never put a credential in the `crank-agent-config` ConfigMap or in any
  file in the repository.** The repository is public. It would not work
  either: the Secret comes after the ConfigMap in `envFrom`, so its (blank)
  value wins.
- Put each key in the Secret with the procedure in
  `docs/capability-config-contract.md` ("Populate or rotate a credential"):
  `FIRECRAWL_API_KEY` there, never in the ConfigMap. The value survives every
  deploy because deploys never write an existing Secret.
- `FIRECRAWL_ENABLED` is a non-secret flag that the checked-in
  `k8s/crank-agent-config.yml` does not carry. Add it to that file in a
  pull request. Do not set it only in the cluster: a key the
  file does not carry is never removed by a deploy or by a revert, so it
  would stay on invisibly.

## Step 4: capability flags

The flags are changed by commits to `k8s/crank-agent-config.yml`, one pull request each, in this
order, following "Durable enablement rule" in `docs/rollout-gates.md`:

1. `AGENT_RUN_ENABLED: "true"` — the master flag, on
   its own. It starts no work by itself.
2. `JOB_PIPELINE_ENABLED: "true"` — the job-source phase
   this runbook is about.

`CRAWL_CRON_ENABLED` belongs to the organization-crawl phase and is a later,
separate pull request (`docs/runbook-crawl-scheduling.md`); it is not part of
the first job inventory.

After each merge, run the post-merge check in that rule: restart the web
Deployment, wait for the rollout, and require `GET /healthz/ready/` to return
HTTP 200 with the capability `enabled: true` and `ok: true`. A 503 means a
required setting is missing, and a restart does not fix it.

An edit made only in the cluster is reverted by the next merge to `main`,
because both deploy workflows re-apply that file. The database
`CapabilitySwitch` (`job_pipeline`, and `crawl_schedule` for the organization
crawl) is the control that no deploy overwrites; it can only turn a
capability off.

## Step 5: run the first crawl batch

**Steps 3 and 4 come first.** Without them `trigger_crawl` is refused for
every seeded source — `USAJOBS Search` needs `USAJOBS_AUTH_KEY` and
`USAJOBS_USER_AGENT_EMAIL`, and the three `firecrawl-careers` sources need
`FIRECRAWL_ENABLED` and `FIRECRAWL_API_KEY` — and `run_job_pipeline` prints
`job_pipeline: disabled; no work performed` and exits 0 without crawling
anything.

Trigger one source at a time for a controlled smoke test:

```sh
# Trigger a single job-source crawl (requires --confirm)
python manage.py trigger_crawl --source-key "USAJOBS Search" --source-type job --confirm

# Check the result
python manage.py crawl_status
```

If the first source succeeds, trigger the remaining sources or run the
scheduler for a batch:

```sh
python manage.py run_job_pipeline
```

## Step 6: verify listing counts

Until step 5 has run, every source shows zero listings here.

```sh
python manage.py crawl_status
```

Each source that completed successfully should show a non-zero listing count
and a recent last-crawl timestamp.

To include closed/expired listings in the count:

```sh
python manage.py crawl_status --include-closed
```

## Step 7: the job-pipeline CronJob

Both deploy workflows apply `deploy/cronjob-job-pipeline.yaml` on every run,
substituting the image tag (the built commit for `deploy-home`, `latest` for
a manifest-only `update-home-deployment`), so `crank-job-pipeline` exists,
suspended, and its pods receive `crank-capability-secrets`. Check:

```sh
kubectl -n crank get cronjob crank-job-pipeline
```

Do not `kubectl apply -f` the file directly: its image tag is the literal
`${GITHUB_SHA}` until substituted.

After the smoke test passed, unsuspend it by committing `spec.suspend: false`
in `deploy/cronjob-job-pipeline.yaml`. A `kubectl patch` of `suspend` is
reverted by the next deploy. That path is in the `update-home-deployment`
trigger list and in the `build-image` ignore list, so the commit deploys
without building an image. Whether a run does any work still depends on the
flags in the re-applied ConfigMap (step 4) and on the `job_pipeline` switch.

Leave `crank-crawl-organizations` suspended until organization-profile sources
are separately seeded and smoke-tested. It is defined in
`k8s/crank-crawl-cron.yaml`, which every deploy re-applies, so a
`kubectl patch` on it lasts only until the next merge to `main`: unsuspend it
by committing `spec.suspend: false` in that file. **Do not unsuspend it until
https://github.com/norcalipa/crank/issues/615 is fixed**: that manifest lacks
the `fats` `hostAliases` and the `dnsPolicy`/`dnsConfig` block, so its pods
cannot reach the database or external hosts.

## Step 8: enable recurring inventory monitoring

The read-only health probe reports zero enabled sources, zero active listings,
stale sources, repeated crawl failures, listing collapse, and unregistered
adapters. It is safe to run at any time and never needs provider credentials:

```sh
# Local/one-off check (exits 1 when unhealthy)
python manage.py crawl_healthcheck
```

**Leave the recurring probe suspended until step 6 shows listings.** The probe itself needs no credential and no capability flag, but
it exits 1 whenever the inventory is unhealthy, and with no enabled source or
no active listing it always is. Unsuspended early it would give a
`crank-healthcheck` Job that fails every 15 minutes, `zero-enabled-sources`
or `zero-active-listings` open from then on, and a first
`job-source-alerts-quiet` window that breaches by construction, because an
alert already open when a window starts counts against it
(`docs/monitoring.md`, "Release decision gates", what "value" means). The manifest's own header says the
same: unsuspend only after the bootstrap is complete and the alerts are
wired to a policy.

Once the inventory exists and the owner has created the alert
policy, commit `spec.suspend: false` in `k8s/crank-healthcheck-cron.yaml`. **Do not
unsuspend it until https://github.com/norcalipa/crank/issues/615 is fixed**
(the manifest lacks `hostAliases` and `dnsConfig`).
The deploy
workflows already apply that manifest on every deploy (substituting the image
tag for `${GITHUB_SHA}`), so the `crank-healthcheck` CronJob exists,
suspended, and the merge unsuspends it. Do not `kubectl apply -f` the file directly: its image tag is the literal
`${GITHUB_SHA}` until substituted. A `kubectl patch` of `suspend` is reverted
by the next deploy. Confirm with:

```sh
kubectl -n crank get cronjob crank-healthcheck
```

The probe emits an `inventory_health` New Relic event. `docs/monitoring.yaml`
defines the alert queries that read it (zero-enabled-sources,
zero-active-listings, stale-inventory, repeated-failures, listing-collapse),
but nothing in the repository creates the alert policy: until the owner
creates it in the alerting tool, no alert fires.

If the probe itself cannot run (for example the database is unreachable), it
emits a degraded `inventory_health` event with `healthy = false` and a
`reason_code`, and the CronJob fails so `failedJobsHistoryLimit` retains
evidence. Keep Kubernetes-level alerting on CronJob failures enabled so
infrastructure outages surface even when the event pipeline is down.

## Verify with the readiness panel

Open **Job Retrieval Operations** in Django admin (staff only). The
"Next step" callout names the first unmet prerequisite (a blocker is never
displaced by a warning); only when nothing is unmet does it name the first stage
that needs attention. The "End-to-end readiness" list shows every stage with a text status. The panel
is read-only: it never calls a provider and only reports whether a credential
is present, never its value.

| Panel stage | Runbook step |
| --- | --- |
| Approved and enabled source | Step 1: seed job sources |
| Registered adapter and allowlisted URL | Step 1: seed job sources |
| Source credentials present | Step 3: source credentials |
| Job pipeline capability enabled | Step 4: capability flags |
| Recent pipeline run finished | Step 7: the job-pipeline CronJob |
| Queued run consumed | [Crawl scheduling: queued runs are consumed](runbook-crawl-scheduling.md#queued-runs-are-consumed-issue-462) |
| Committed listing inventory | Step 6: verify listing counts |
| Employers resolved | Step 6: verify listing counts |
| Current matches | [Match recompute: rollout order](match-recompute.md#rollout-order) |

A queued run is shown as "Queued — waiting for a consumer" with no counts until the
pipeline consumer claims it. "Run progress" reports the last completed run's
counts, and "Backlog" reports unresolved employers, pending company-profile
review, the publication outbox and match lag.

## Rollback

If something goes wrong:

1. **Kill switches (immediate)**: in Django admin, set the database
   `CapabilitySwitch` for `job_pipeline` (and `crawl_schedule` for the
   organization crawl) to disabled. The commands check it on every run, it
   takes effect without a deploy, and no deploy overwrites it.

2. **Suspend CronJobs**: every deploy re-applies the CronJob manifests, so
   a `kubectl patch` of `suspend` is undone by the next deploy. If
   `spec.suspend: false` was committed in
   `deploy/cronjob-job-pipeline.yaml`, `k8s/crank-crawl-cron.yaml` or
   `k8s/crank-healthcheck-cron.yaml`, revert that commit; the deploy
   suspends the CronJob again.

3. **Make it durable**: if a flag was committed as `"true"` in
   `k8s/crank-agent-config.yml`, revert the commit for the phase being rolled
   back (`JOB_PIPELINE_ENABLED`, or `CRAWL_CRON_ENABLED`). Revert the master
   flag `AGENT_RUN_ENABLED` last, and only when every flag that depends on it
   is `"false"` again: with `JOB_PIPELINE_ENABLED` or `CRAWL_CRON_ENABLED`
   still true and the master flag off, `GET /healthz/ready/` returns 503 and
   new web pods fail their readiness probe. Setting a flag to `false` only in
   the cluster lasts until the next merge to `main` re-applies the file.

4. **Disable Firecrawl**: if `FIRECRAWL_ENABLED` was committed, revert that
   commit so the adapter refuses to construct. If it was ever set only in
   the cluster, remove the key there by hand: no deploy removes it.

5. **Clear partial data safely**: to remove all listings from a single source
   without affecting others, use the Django admin or a shell:

   ```sh
   python manage.py shell -c "
   from crank.models.job import JobSourceCatalog, JobListing
   source = JobSourceCatalog.objects.get(name='USAJOBS Search')
   JobListing.all_objects.filter(source=source).delete()
   source.last_crawl_at = None
   source.last_attempt_at = None
   source.consecutive_failures = 0
   source.save(update_fields=['last_crawl_at', 'last_attempt_at', 'consecutive_failures'])
   "
   ```

   To clear all job listings and reset crawl timestamps:

   ```sh
   python manage.py shell -c "
   from crank.models.job import JobListing, JobSourceCatalog
   JobListing.all_objects.all().delete()
   JobSourceCatalog.objects.all().update(
       last_crawl_at=None, last_attempt_at=None, consecutive_failures=0
   )
   "
   ```

5. **Remove seeded sources** (if needed):

   ```sh
   python manage.py shell -c "
   from crank.models.job import JobSourceCatalog
   JobSourceCatalog.objects.all().delete()
   "
   ```

   Re-running `seed_job_sources` will recreate them idempotently.

## Idempotency

All commands are safe to re-run:

- `seed_job_sources` upserts rows by name and creates new rows as pending/disabled. Re-seeding preserves operator-set approval_state and enabled fields on existing rows.
- `trigger_crawl` rejects a second concurrent crawl for the same source.
- `run_job_pipeline` skips sources that are not approved+enabled, or that
  succeeded within `JOB_REFRESH_TTL_HOURS`, or are in retry backoff after
  failures. Only successful fetches advance the freshness clock, so re-running
  is safe and never re-fetches a fresh source.
- `crawl_status` and `crawl_healthcheck` are read-only.
