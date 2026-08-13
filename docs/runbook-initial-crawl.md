<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

Owner: maintainer (crank.fyi)
Last reviewed: 2026-08-12
Version/change process: update this runbook with every initial-crawl or seeding-policy change.

# Runbook: initial job inventory crawl

This runbook takes production from zero `JobListing` rows to a populated
inventory. It assumes the Firecrawl adapter (#365), profile crawler (#366),
scheduler (#367), and admin trigger (#368) are already merged and deployed.

## Prerequisites

- Kubernetes access to the `crank` namespace
- `FIRECRAWL_API_KEY` stored in the `crank-secrets` Kubernetes Secret
- `AGENT_RUN_ENABLED` and `CRAWL_CRON_ENABLED` currently `false` (defaults)
- CronJobs `crank-crawl-organizations` and `crank-crawl-jobs` exist but are
  suspended

## Default budget guardrails

| Setting | Default | Purpose |
| --- | --- | --- |
| `FIRECRAWL_MAX_PAGES` | 10 | Max pages per Firecrawl crawl |
| `FIRECRAWL_MAX_LISTINGS` | 100 | Max listings per crawl |
| `FIRECRAWL_CREDIT_BUDGET` | 10 | Firecrawl credits per crawl |
| `CRAWL_MAX_SOURCES` | 10 | Sources per schedule dispatch |
| `CRAWL_MAX_JOB_LISTINGS` | 100 | Listings per job-source dispatch |
| `CRAWL_MAX_PAGES` | 10 | Pages per job-source dispatch |
| `CRAWL_DEADLINE_SECONDS` | 300 | Wall-clock budget per dispatch |
| `JOB_FRESHNESS_HOURS` | 24 | Stale threshold for job sources |

For the first production crawl, keep the defaults. One full schedule dispatch
costs at most `CRAWL_MAX_SOURCES × FIRECRAWL_CREDIT_BUDGET` Firecrawl credits
(10 × 10 = 100 credits) and fetches at most `CRAWL_MAX_SOURCES ×
CRAWL_MAX_JOB_LISTINGS` listings (10 × 100 = 1,000 listings). Adjust
`FIRECRAWL_CREDIT_BUDGET` down to 5 for a cheaper smoke test.

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

## Step 3: set the Firecrawl API key

Ensure the environment has the key:

```sh
# In the crank-agent-config ConfigMap or deployment env:
FIRECRAWL_API_KEY=<your-key>
FIRECRAWL_ENABLED=true
```

## Step 4: enable capability switches

```sh
# In crank-agent-config:
AGENT_RUN_ENABLED=true
CRAWL_CRON_ENABLED=true
JOB_PIPELINE_ENABLED=true
```

Deploy the config change so pods pick up the new values.

## Step 5: run the first crawl batch

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
python manage.py schedule_crawls --phase jobs --max-sources 3 --deadline-seconds 120
```

## Step 6: verify listing counts

```sh
python manage.py crawl_status
```

Each source that completed successfully should show a non-zero listing count
and a recent last-crawl timestamp.

To include closed/expired listings in the count:

```sh
python manage.py crawl_status --include-closed
```

## Step 7: unsuspend CronJobs

Once you have confirmed listings exist and the smoke test passed:

```sh
kubectl -n crank patch cronjob crank-crawl-jobs -p '{"spec":{"suspend":false}}'
```

Leave `crank-crawl-organizations` suspended until organization-profile sources
are separately seeded and smoke-tested.

## Step 8: enable recurring inventory monitoring

The read-only health probe reports zero enabled sources, zero active listings,
stale sources, repeated crawl failures, listing collapse, and unregistered
adapters. It is safe to run at any time and never needs provider credentials:

```sh
# Local/one-off check (exits 1 when unhealthy)
python manage.py crawl_healthcheck

# Recurring probe: unsuspend the healthcheck CronJob
kubectl -n crank patch cronjob crank-healthcheck -p '{"spec":{"suspend":false}}'
```

The probe emits an `inventory_health` New Relic event; the alert policy in
`docs/monitoring.yaml` (zero-enabled-sources, zero-active-listings,
stale-inventory, repeated-failures, listing-collapse) fires from that event.

## Rollback

If something goes wrong:

1. **Kill switches**: set `CRAWL_CRON_ENABLED=false` and
   `AGENT_RUN_ENABLED=false`. CronJobs stop dispatching immediately.

2. **Suspend CronJobs**:

   ```sh
   kubectl -n crank patch cronjob crank-crawl-jobs -p '{"spec":{"suspend":true}}'
   kubectl -n crank patch cronjob crank-crawl-organizations -p '{"spec":{"suspend":true}}'
   ```

3. **Disable Firecrawl**: set `FIRECRAWL_ENABLED=false` so the adapter refuses
   to construct.

4. **Clear partial data safely**: to remove all listings from a single source
   without affecting others, use the Django admin or a shell:

   ```sh
   python manage.py shell -c "
   from crank.models.job import JobSourceCatalog, JobListing
   source = JobSourceCatalog.objects.get(name='USAJOBS Search')
   JobListing.all_objects.filter(source=source).delete()
   source.last_crawl_at = None
   source.save(update_fields=['last_crawl_at'])
   "
   ```

   To clear all job listings and reset crawl timestamps:

   ```sh
   python manage.py shell -c "
   from crank.models.job import JobListing, JobSourceCatalog
   JobListing.all_objects.all().delete()
   JobSourceCatalog.objects.all().update(last_crawl_at=None)
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

- `seed_job_sources` upserts rows by name.
- `trigger_crawl` rejects a second concurrent crawl for the same source.
- `schedule_crawls` skips sources that are fresh (within `JOB_FRESHNESS_HOURS`)
  or disabled.
- `crawl_status` and `crawl_healthcheck` are read-only.
