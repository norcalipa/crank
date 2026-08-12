<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->
# Job-pipeline resilience and capacity benchmark

The scheduler's Phase 4 benchmark is deterministic and offline. It generates
source pages, replays bounded transient failures, resolves synthetic
organizations, ranks synthetic listings for a bounded user population, and
invalidates synthetic cache entries. It does **not** open a socket, require a
credential, or write the application database.

## Repeatable commands

Install the repository's development dependencies, then run the CI-sized gate:

```sh
ENV=dev SECRET_KEY=test python manage.py benchmark_job_pipeline \
  --profile ci --seed 324 --assert-budgets
```

For a larger staging rehearsal (still offline):

```sh
ENV=dev SECRET_KEY=test python manage.py benchmark_job_pipeline \
  --profile staging --seed 324 --assert-budgets
```

The command prints JSON containing generated counts, retry counts, per-stage
wall timings, CPU time, peak traced allocations, query/external-call counts,
and budget failures. Keep the seed fixed when comparing two revisions. The
benchmark intentionally reports `query_count: 0` and `external_calls: 0`: those
are budgets for this no-network harness, not a claim about a live deployment.

## Workloads and budgets

| Profile | Sources | Pages/source | Listings/page | Users | Run window | CPU | Peak memory | Queries | External calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `ci` | 2 | 2 | 50 | 4 | 2 s | 2 s | 128 MiB | 0 | 0 |
| `staging` | 3 | 3 | 100 | 10 | 10 s | 10 s | 512 MiB | 0 | 0 |

Each source has one synthetic transient failure. Retry attempts are capped at
three; the benchmark asserts that the profile completes after one retry per
source. Permanent failure, duplicate/replay, deadline, stale-lock, overlap,
and partial-failure behavior is exercised by the unit tests around the real
pipeline and run lifecycle:

- `crank/tests/services/test_job_pipeline.py` covers source/user isolation,
  deadline interruption, bounded source/user/listing selection, and replay.
- `crank/tests/test_agent_runs.py` covers overlap claims and stale-lock
  reclamation.
- `crank/tests/agents/sources/test_transport.py` covers transient retry
  backoff, exhaustion, throttling, and permanent failures.
- `crank/tests/agents/test_job_ingest.py` and
  `crank/tests/agents/test_match_persist.py` cover idempotent replay and
  duplicate-safe matching persistence.
- `crank/tests/services/test_score_persistence.py` covers transaction-safe
  cache invalidation and database contention behavior for the shared
  persistence pattern.

## Capacity assumptions and scaling triggers

The current scheduled pipeline is deliberately bounded by
`JOB_PIPELINE_MAX_SOURCES`, `JOB_PIPELINE_MAX_USERS`,
`JOB_PIPELINE_MAX_LISTINGS_PER_USER`, and `JOB_PIPELINE_DEADLINE_SECONDS`.
Sources are isolated, matching is deterministic, and a run claims one
`AgentRun` slot per run type. The current design assumes a single scheduled
worker can finish the configured batch in one run window and that the database
can sustain the per-listing upsert and per-user match transaction load.

Scale up or split work when any of these conditions occurs in two consecutive
staging runs:

1. `budget_passed` is false, or any stage consumes more than 80% of its budget;
2. the pipeline reaches a source/user/listing cap or deadline before processing
   the eligible population;
3. retries or partial failures leave a growing backlog across runs; or
4. database lock wait/deadlock errors appear or match persistence dominates the
   run window.

The next bounded step is partitioning by source and user batches with durable
progress/cursors and per-partition locks; do not increase limits blindly.
Before production rollout, replace the synthetic zero-query/zero-network
budgets with staging-observed database query and external-call budgets while
keeping live-source tests disabled in CI.
