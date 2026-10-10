<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Staged Rollout Gates and Rollback Validation

**Issue:** [#328](https://github.com/norcalipa/crank/issues/328)
**Phase:** 4 — Hardening and Rollout
**Status:** Draft

## Purpose

This document defines the evidence-based gates, named owners, observation
windows, and rollback procedures for each independent capability:
**interactive agent**, **score source**, and **job source**.

Each capability progresses through four stages — **staging**, **internal
canary**, **limited production**, and **general availability** — only after
the prior stage passes all measured gates. No two capabilities share a canary
decision; each is evaluated independently.

---

## Capability: Interactive Agent

### Settings and Kill Switches

| Gate Layer | Setting / Switch | Default |
|---|---|---|
| Master switch | `AGENT_RUN_ENABLED` | `False` |
| Per-command | `AGENT_NOOP_ENABLED` | `False` |
| Runtime kill switch | `CapabilitySwitch(key="interactive_agent")` | `enabled=True` (but master+per-command off) |

### Stage 1: Staging

| Field | Value |
|---|---|
| Owner | Engineering Lead |
| Approver(s) | Tech Lead, Security Lead |
| Observation window | 48 hours |
| Success threshold | Zero `FAILED` runs; zero unhandled exceptions |
| Error threshold | < 1% error rate |
| Latency threshold | p95 < 5 s end-to-end |
| Cost threshold | < $1/day estimated provider cost |
| Privacy sign-off | Required before canary |
| Accessibility sign-off | Required before canary |
| Source-policy confirmation | N/A (no external source) |
| Pass/Fail evidence | `AgentRun` records (status, counts, error_summary) |

### Stage 2: Internal Canary

| Field | Value |
|---|---|
| Owner | Engineering Lead |
| Approver(s) | Tech Lead, Operations Lead |
| Observation window | 72 hours |
| Success threshold | 3 consecutive successful runs |
| Error threshold | 0 errors across canary window |
| Latency threshold | p95 < 4 s |
| Cost threshold | < $5/day |
| Privacy sign-off | Confirmed in Stage 1 |
| Accessibility sign-off | Confirmed in Stage 1 |
| Source-policy confirmation | N/A |
| Pass/Fail evidence | `AgentRun` records + New Relic dashboard |

### Stage 3: Limited Production

| Field | Value |
|---|---|
| Owner | Operations Lead |
| Approver(s) | Tech Lead, Security Lead, Privacy Lead |
| Observation window | 7 days |
| Success threshold | 90%+ success rate over window |
| Error threshold | < 0.5% error rate |
| Latency threshold | p95 < 5 s |
| Cost threshold | < $20/day |
| Freshness threshold | Last run within 2× cadence interval |
| Privacy sign-off | Required (stage-level) |
| Accessibility sign-off | Required (stage-level) |
| Pass/Fail evidence | `AgentRun` + `OperationalChangeAudit` + monitoring dashboard |

### Stage 4: General Availability

| Field | Value |
|---|---|
| Owner | Operations Lead |
| Approver(s) | Tech Lead, Security Lead, Privacy Lead, Accessibility Lead |
| Observation window | 30 days |
| Success threshold | 95%+ success rate |
| Error threshold | < 0.1% error rate |
| Latency threshold | p95 < 5 s |
| Cost threshold | < $50/day |
| Freshness threshold | Last run within 2× cadence interval |
| Pass/Fail evidence | `AgentRun` trends + `OperationalChangeAudit` history |

### Rollback Procedure

1. Set `CapabilitySwitch(key="interactive_agent", enabled=False)` via admin
   (the changelist action prompts an intermediate confirmation page before it
   applies; the page re-POSTs `confirm=yes` on confirm).
2. Set `AGENT_RUN_ENABLED=False` and `AGENT_NOOP_ENABLED=False` in the
   ConfigMap.
3. Verify no new `AgentRun` rows are created after disablement.
4. Verify existing data remains consistent (no orphaned RUNNING runs beyond
   stale-lock TTL).
5. `OperationalChangeAudit` records the rollback action with actor and
   confirmed flag.

---

## Capability: Score Source (Gather Scores)

### Settings and Kill Switches

| Gate Layer | Setting / Switch | Default |
|---|---|---|
| Master switch | `AGENT_RUN_ENABLED` | `False` |
| Per-command | `GATHER_SCORES_ENABLED` | `False` |
| Runtime kill switch | `CapabilitySwitch(key="gather_scores")` | `enabled=True` (but master+per-command off) |
| Source-level | `SourceCatalog.enabled` + `SourceCatalog.approval_state` | `False` / `pending` |

### Stage 1: Staging

| Field | Value |
|---|---|
| Owner | Data Engineering Lead |
| Approver(s) | Tech Lead, Security Lead |
| Observation window | 48 hours |
| Success threshold | All approved sources return valid observations |
| Error threshold | 0 source failures |
| Latency threshold | Per-source timeout < `GATHER_SCORES_SOURCE_TIMEOUT_SECONDS` |
| Freshness threshold | `SourceCatalog.last_success_at` within cadence |
| Cost threshold | < $2/day estimated provider cost |
| Privacy sign-off | Required before canary |
| Accessibility sign-off | Required before canary |
| Source-policy confirmation | `SourceCatalog.terms_reviewed=True` and `approval_state=approved` |
| Pass/Fail evidence | `SourceRun` records (per-source status, counts, freshness) |

### Stage 2: Internal Canary

| Field | Value |
|---|---|
| Owner | Data Engineering Lead |
| Approver(s) | Tech Lead, Operations Lead |
| Observation window | 72 hours |
| Success threshold | 3 consecutive successful runs with nonzero observations |
| Error threshold | 0 source failures across canary |
| Latency threshold | Per-source < 90 s |
| Freshness threshold | `last_success_at` updated each run |
| Cost threshold | < $10/day |
| Privacy sign-off | Confirmed in Stage 1 |
| Accessibility sign-off | Confirmed in Stage 1 |
| Source-policy confirmation | `SourceCatalog` approval current |
| Pass/Fail evidence | `SourceRun` + `AgentRun` + New Relic |

### Stage 3: Limited Production

| Field | Value |
|---|---|
| Owner | Operations Lead |
| Approver(s) | Tech Lead, Security Lead, Privacy Lead |
| Observation window | 7 days |
| Success threshold | 90%+ source success rate |
| Error threshold | < 5% source failure rate |
| Latency threshold | Per-source < 120 s |
| Freshness threshold | `last_success_at` within 2× cadence |
| Cost threshold | < $30/day |
| Privacy sign-off | Required (stage-level) |
| Accessibility sign-off | Required (stage-level) |
| Pass/Fail evidence | `SourceRun` + `AgentRun` + `OperationalChangeAudit` |

### Stage 4: General Availability

| Field | Value |
|---|---|
| Owner | Operations Lead |
| Approver(s) | Tech Lead, Security Lead, Privacy Lead |
| Observation window | 30 days |
| Success threshold | 95%+ source success rate |
| Error threshold | < 1% source failure rate |
| Latency threshold | Per-source < 120 s |
| Freshness threshold | `last_success_at` within 2× cadence |
| Cost threshold | < $100/day |
| Pass/Fail evidence | `SourceRun` trends + audit history |

### Rollback Procedure

1. Set `CapabilitySwitch(key="gather_scores", enabled=False)` via admin.
2. Set `GATHER_SCORES_ENABLED=False` in ConfigMap.
3. Set `SourceCatalog.enabled=False` for affected sources.
4. Verify no new `SourceRun` or `AgentRun` rows after disablement.
5. Verify retained score data is consistent (no partial writes; committed
   observations remain valid).
6. `OperationalChangeAudit` records each change.

---

## Capability: Job Source (Job Pipeline)

### Settings and Kill Switches

| Gate Layer | Setting / Switch | Default |
|---|---|---|
| Master switch | `AGENT_RUN_ENABLED` | `False` |
| Per-command | `JOB_PIPELINE_ENABLED` | `False` |
| Runtime kill switch | `CapabilitySwitch(key="job_pipeline")` | `enabled=True` (but master+per-command off) |
| Source-level | `JobSourceCatalog.enabled` + `JobSourceCatalog.approval_state` | `False` / `pending` |

### Stage 1: Staging

| Field | Value |
|---|---|
| Owner | Data Engineering Lead |
| Approver(s) | Tech Lead, Security Lead |
| Observation window | 48 hours |
| Success threshold | All approved sources ingest without errors |
| Error threshold | 0 source failures |
| Latency threshold | Pipeline completes within `JOB_PIPELINE_DEADLINE_SECONDS` |
| Freshness threshold | `JobListing.last_seen_at` updated within cadence |
| Cost threshold | < $2/day estimated provider cost |
| Privacy sign-off | Required before canary |
| Accessibility sign-off | Required before canary |
| Source-policy confirmation | `JobSourceCatalog.approval_state=approved` and `enabled=True` |
| Pass/Fail evidence | `AgentRun` counts (listings_ingested, listings_updated, matches_persisted) |

### Stage 2: Internal Canary

| Field | Value |
|---|---|
| Owner | Data Engineering Lead |
| Approver(s) | Tech Lead, Operations Lead |
| Observation window | 72 hours |
| Success threshold | 3 consecutive successful pipeline runs |
| Error threshold | 0 source failures; 0 user failures |
| Latency threshold | Pipeline < 300 s |
| Freshness threshold | `JobListing.last_seen_at` updated each run |
| Cost threshold | < $10/day |
| Privacy sign-off | Confirmed in Stage 1 |
| Accessibility sign-off | Confirmed in Stage 1 |
| Source-policy confirmation | `JobSourceCatalog` approval current |
| Pass/Fail evidence | `AgentRun` + New Relic matching_batch events |

### Stage 3: Limited Production

| Field | Value |
|---|---|
| Owner | Operations Lead |
| Approver(s) | Tech Lead, Security Lead, Privacy Lead |
| Observation window | 7 days |
| Success threshold | 90%+ source success rate; 90%+ user match success |
| Error threshold | < 5% source failure; < 5% user failure |
| Latency threshold | Pipeline < 300 s |
| Freshness threshold | `last_seen_at` within 2× cadence |
| Cost threshold | < $30/day |
| Privacy sign-off | Required (stage-level) |
| Accessibility sign-off | Required (stage-level) |
| Pass/Fail evidence | `AgentRun` + `OperationalChangeAudit` |

### Stage 4: General Availability

| Field | Value |
|---|---|
| Owner | Operations Lead |
| Approver(s) | Tech Lead, Security Lead, Privacy Lead |
| Observation window | 30 days |
| Success threshold | 95%+ source success; 95%+ user match success |
| Error threshold | < 1% source failure; < 1% user failure |
| Latency threshold | Pipeline < 300 s |
| Freshness threshold | `last_seen_at` within 2× cadence |
| Cost threshold | < $100/day |
| Pass/Fail evidence | `AgentRun` trends + audit history |

### Rollback Procedure

1. Set `CapabilitySwitch(key="job_pipeline", enabled=False)` via admin.
2. Set `JOB_PIPELINE_ENABLED=False` in ConfigMap.
3. Set `JobSourceCatalog.enabled=False` for affected sources.
4. Verify no new `AgentRun` rows after disablement.
5. Verify `JobListing` and `JobMatch` data remains consistent (no orphaned
   matches; committed listings retain valid status).
6. `OperationalChangeAudit` records each change.

---

## Rollback Ownership

| Capability | Rollback Owner | Escalation |
|---|---|---|
| Interactive Agent | Operations Lead | Engineering Lead |
| Score Source | Operations Lead | Data Engineering Lead |
| Job Source | Operations Lead | Data Engineering Lead |

## Rollback Drill

A management command (`rollback_drill`) is provided to rehearse the rollback
procedure in staging. The drill:

1. Disables capability switches and verifies `get_enabled()` returns False.
2. Asserts no new `AgentRun` rows are created after disablement.
3. Verifies existing data remains consistent (no orphaned RUNNING runs).
4. Records an `OperationalChangeAudit` entry for the drill.
5. Emits a monitoring event for the rollback drill.
6. Reports a JSON summary suitable for dashboard capture.

Run the drill:

```bash
python manage.py rollback_drill
python manage.py rollback_drill --json
```

## Capability Registry (Epic #454 — issue #463)

Every capability introduced by epic #454 ships with an independently
controlled switch, default **off**, added to `ALLOWED_CAPABILITY_KEYS` in
`crank/models/monitoring.py` (or a settings flag in
`crank/settings/base.py`) **by its owning ticket before its code path is
enabled anywhere**. Names marked **planned** below are reserved by this
registry only; per #463, new flag names must be implemented before being
documented as available. `rollback_drill` derives its capability list in
lockstep from `ALLOWED_CAPABILITY_KEYS`
(`rollback_drill.drill_capabilities()`), and
`crank/tests/test_rollback_drill.py` plus `crank/tests/test_rollout_gates.py`
fail if a registered key is missing from the drill. The drill also maps
every registered key to the **real execution gate its production path
consults** (`rollback_drill.gate_verifiers()`): with the switch disabled, the
drill invokes that production gate (settings flags forced on, so the switch
is the only variable) and requires it to block. A registered key whose
real gate is missing or not enforced **cannot report `passed`** — it fails
the drill — so a registry row always corresponds to a path that actually
reads its switch.

| Capability | Switch key / settings flag | Owning ticket | Default | Status |
|---|---|---|---|---|
| Interactive agent | `interactive_agent` + `AGENT_RUN_ENABLED`, `AGENT_NOOP_ENABLED` | #328 | off | registered |
| Score source | `gather_scores` + `GATHER_SCORES_ENABLED` | #328 | off | registered |
| Job pipeline | `job_pipeline` + `JOB_PIPELINE_ENABLED` | #328 | off | registered |
| Agent no-op | `agent_noop` + `AGENT_NOOP_ENABLED` | #328 | off | registered |
| Crawl scheduling | `crawl_schedule` + `CRAWL_CRON_ENABLED` | #328 | off | registered |
| On-demand crawl | `crawl` | #328 | off | registered |
| Publication consumer | `publication_consumer` + `PUBLICATION_CONSUMER_ENABLED` | #470 | off | registered |
| Assistant shell | `assistant_shell` | #472 | off | planned |
| Match recompute | `match_recompute` + `MATCH_RECOMPUTE_ENABLED` | #475 | off | registered (also gates the user refresh, `POST /api/job-matches/refresh/`, #473; see docs/match-recompute.md) |
| Match results read | `match_results_read` + `MATCH_RESULTS_READ_ENABLED` | #475 | off | registered |

Independence rules: no two capabilities share a canary decision or a switch;
flipping one switch never disables another capability's data path; a
rollback leaves direct controls and stored conversations, preferences, and
accepted data usable, and never deletes records or reverses production
migrations (see `docs/deployment-migrations.md`, "Epic #454 rollout").

## Contextual assistant staged release (#492)

This section defines how the epic #454 capabilities are released in separate,
independently reversible phases, and which measured gates each decision uses.
It records a **procedure, not a verified state**: nothing here claims that any
phase is enabled, observed or approved. Operational activation (credentials,
bootstrap, first crawl) is the procedure in
[#453](https://github.com/norcalipa/crank/issues/453); this section adds the
decision gates and the record that go around it. The moderated usability round
that feeds the shell decision is defined in `docs/usability-validation.md`,
which also holds the single list of
[decisions pending owner confirmation](usability-validation.md#decisions-pending-owner-confirmation).

### Observed state when this section was written

| Fact | Observation | Source |
|---|---|---|
| Capabilities reported by production | `interactive_agent`, `job_pipeline` and `crawl` each `enabled: false`; `all_ok: true`; `pending_migrations: 0`. Observed twice on 2026-10-06, at 15:50:56Z and 16:31:54Z. | Public readiness endpoint, `GET https://crank.fyi/healthz/ready/` (read-only) |
| Flags and CronJobs are re-applied on every deploy | `k8s/crank-agent-config.yml` (every capability flag `"false"`) and the CronJob manifests `k8s/crank-cronjob.yml`, `k8s/crank-crawl-cron.yaml`, `k8s/crank-healthcheck-cron.yaml` and `k8s/cron-gather-scores.yml` (the last three `suspend: true`) are applied with `kubectl apply` by both deploy workflows. | `.github/workflows/deploy-home.yml` (runs after every successful `Build Image` on `main`) and `.github/workflows/update-home-deployment.yml` (runs when one of those manifests changes on `main`) |
| Capability credentials are blanked on every deploy | Both deploy workflows run `kubectl create secret generic crank-capability-secrets --from-literal=LLM_API_KEY='' … --dry-run=client -o yaml` piped into `kubectl apply -f -` on every run, with five empty literals (`LLM_API_KEY`, `YELP_API_KEY`, `USAJOBS_AUTH_KEY`, `USAJOBS_USER_AGENT_EMAIL`, `FIRECRAWL_API_KEY`). `kubectl apply` sets every key the applied object carries, so each run replaces whatever an operator populated with the empty string. The comment above that step ("never overwrite values an operator has already populated") and `docs/capability-config-contract.md` say the opposite. Tracked in [#555](https://github.com/norcalipa/crank/issues/555). | `.github/workflows/deploy-home.yml` and `.github/workflows/update-home-deployment.yml`, step "Deploy to Kubernetes" |
| The readiness probe fails on an enabled, unconfigured capability | `GET /healthz/ready/` returns HTTP 503 `not_ready` whenever the capability report is not `all_ok`, for example `INTERACTIVE_AGENT_ENABLED` true with an empty `LLM_API_KEY`, or `JOB_PIPELINE_ENABLED` or `CRAWL_CRON_ENABLED` true with `AGENT_RUN_ENABLED` false. That endpoint is the web Deployment's `readinessProbe`. Neither deploy workflow waits for the rollout. | `crank/views/health.py`, `crank/capability.py`, `k8s/crank.yml`; `crank/tests/test_release_decision_gates.py` runs the endpoint in both configurations |
| The job-pipeline and match-recompute CronJobs are not created by any deploy | `deploy/cronjob-job-pipeline.yaml` (`crank-job-pipeline`) and `deploy/cronjob-match-recompute.yaml` (`crank-match-recompute`) are applied by neither workflow, and their image tag is the literal `${GITHUB_SHA}`, so the files are not valid manifests until that is substituted. Their `envFrom` lists `crank-config`, `crank-agent-config` and `db-connect-credentials` but not `crank-capability-secrets`, so a pod created from them receives no source credential. Tracked in #555. | the two files; `grep -n deploy/ .github/workflows/*.yml` returns nothing |
| Staging | The repository defines no staging deployment: `crank/settings/staging.py` and `seed_staging_baseline` exist, but no manifest, workflow or compose file names a staging environment. Whether one exists outside the repository is not known from the repository. | `grep -rn staging k8s deploy .github docker-compose.yml` returns nothing |

#453 was closed on 2026-09-18. What its operator did in the cluster cannot be
read from the repository; the first row above is what the public endpoint
reported afterwards.

### Durable enablement rule

**Durable enablement of a capability that needs a credential is not
currently safe. It depends on
[#555](https://github.com/norcalipa/crank/issues/555), which is open. Do not
merge an enablement pull request for any phase after `shell` until #555 is
fixed.** The rest of this subsection explains why, and then describes the
procedure that applies once it is.

**Why a cluster-only change does not last.** The ConfigMap and the `k8s/`
CronJob manifests are re-applied from the repository on every deploy, so an
out-of-band `kubectl patch` or ConfigMap edit of a key those files carry
lasts only until the next merge to `main`. (A key the checked-in file does
not carry — `PUBLICATION_CONSUMER_ENABLED` and `FIRECRAWL_ENABLED` are two —
is the opposite: set out of band it survives every re-apply and no revert
pull request removes it. Never set such a key out of band; add it to
`k8s/crank-agent-config.yml` in the phase's pull request so that the
repository shows it.)

**Why committing the flag is not enough today.** The same deploy step that
re-applies the flags also re-applies `crank-capability-secrets` with empty
values (see "Observed state" above). Until now that was harmless, because the
flags were reverted with the keys. With a flag committed as `"true"` it is
not:

- **`interactive_replies`.** `INTERACTIVE_AGENT_ENABLED` true with an empty
  `LLM_API_KEY` makes `GET /healthz/ready/` return **503**. That endpoint is
  the web readiness probe, so **new web pods never become Ready**. The old
  pods keep serving the old image and the old flags for as long as they
  live; any pod recreated later starts NotReady. **The deploy workflow still
  goes green**, because neither workflow waits for the rollout. This happens
  on the enablement merge and again on every later merge to `main`.
- **`job_source`, `organization_crawl`, `score_source`.** Their source
  credentials (`USAJOBS_AUTH_KEY`, `USAJOBS_USER_AGENT_EMAIL`,
  `FIRECRAWL_API_KEY`, `YELP_API_KEY`) are blanked the same way. The
  readiness probe does not check them, so nothing fails loudly: the sources
  that need a key stop returning data after the next deploy.
- **`publication`, `match_recompute`.** These need no credential of their
  own, but they follow `job_source` in the phase order and consume its
  output, so they wait for the same fix.

Nothing in this repository works around this, and no instruction in this
document or in the crawl runbooks should be read as one. In particular: never
put a credential in a ConfigMap or in the repository (it would also not work:
`crank-capability-secrets` is last in `envFrom` and overrides the ConfigMap
value with the blank one).

**Procedure once #555 is fixed.** These rules assume credentials survive a
deploy and the deploy waits for the rollout. They are not to be followed
before then.

1. One pull request per capability changes that capability's flag in
   `k8s/crank-agent-config.yml` and, where the phase has a CronJob in `k8s/`,
   its `suspend:` line. `update-home-deployment.yml` applies it on merge. The
   shared master flag `AGENT_RUN_ENABLED` has its own pull request (see
   "Shared master flag" under "Phases"). Merge an enablement pull request
   only while no deploy run is in progress: both workflows share
   `concurrency: deployment` with `cancel-in-progress: true`, so the merge
   can cancel a running code deploy and then roll out the previous `latest`
   image.
2. The pull request body is the decision record (template below). It is
   signed the one way decision D5 defines; an approving review is not the
   sign-off (GitHub does not let an account approve its own pull request).
   The body of a pull request in this repository is public: see decision D7
   for what a record may contain.
3. Immediate rollback is the database-backed `CapabilitySwitch` for the phase.
   It survives deploys and is audited by `OperationalChangeAudit`. It is
   followed by a revert pull request so the repository again matches the
   intended state. Revert phase flags in the reverse of the order they were
   enabled, and the master flag last.
4. `deploy/cronjob-job-pipeline.yaml` and `deploy/cronjob-match-recompute.yaml`
   are not applied by either deploy workflow and cannot be applied as they
   stand (see "Observed state"). How those two CronJobs are created, which
   image tag they follow and how they receive source credentials is part of
   #555. Until then the `job_source` and `match_recompute` phases have no
   scheduled run, whatever the flags say.
5. Verify after the merge. An enablement pull request touches only `k8s/`,
   and `build-image.yml` ignores `k8s/**`: **no image is built from the
   enablement merge commit** and `deploy-home.yml` does not run for it. Only
   `update-home-deployment.yml` runs, applying the tag `latest`.
   1. Pods read `envFrom` values only when they start, and applying a changed
      ConfigMap restarts nothing. Unless the apply happened to change the
      Deployment's image tag, no web pod was replaced. Restart them:
      `kubectl -n crank rollout restart deployment/crank`.
   2. Wait for the rollout:
      `kubectl -n crank rollout status deployment/crank --timeout=300s`. If it
      does not complete, the new pods are failing their readiness probe; go
      to step 4.
   3. `GET https://crank.fyi/healthz/ready/` must return **HTTP 200** with
      `"status": "ready"` and `"all_ok": true`, and the capability's entry
      must show both `"enabled": true` and `"ok": true`. `enabled: true`
      alone is the flag, not a working capability. Do not record the phase as
      enabled before all of this holds.
   4. **A 503 is not fixed by another restart.** Read the capability's
      `issues` in the response body: they name what is missing (a credential,
      a model, the master flag). Turn the phase off again — revert the
      enablement pull request — and resolve the cause first.
   5. CronJob pods start fresh on every run, so they read the new values on
      their next run.

### Phases

Phases are enabled in the order of this table. Each has its own switch; no
two share a decision. Every phase after `shell` depends on #555 (see
"Durable enablement rule").

| Phase id | Settings flags | Switch key | Manifest / CronJob | Rollback action |
|---|---|---|---|---|
| `shell` | none — the workspace renders on every page outside the admin, staff and sign-in surfaces (`crank/context_processors.py`, `assistant_workspace_enabled`) | none (`assistant_shell` is reserved as planned, not registered) | web Deployment in `k8s/crank.yml` | Redeploy the previous image tag (`docs/capability-config-contract.md`, "Rollback Procedure", code rollback), then merge a revert commit to `main` — the image rollback alone lasts only until the next merge (see "The shell's rollback is not durable"). Schema changes are additive, so this preserves data (`docs/deployment-migrations.md`, "Epic #454 rollout"). |
| `interactive_replies` | `INTERACTIVE_AGENT_ENABLED`; provider and model settings; `LLM_PRICE_PER_1K_TOKENS_USD` and `LLM_PER_USER_COST_LIMIT_USD`, both above `0` (see "Entry criteria") | `interactive_agent` | `k8s/crank-agent-config.yml`; no CronJob | Disable the switch; revert the enablement PR. Direct priority editing and stored results stay usable. |
| `job_source` | `JOB_PIPELINE_ENABLED` (needs the master flag `AGENT_RUN_ENABLED`); `JobSourceCatalog.enabled` per source | `job_pipeline` | `deploy/cronjob-job-pipeline.yaml` (`crank-job-pipeline`) — created by no deploy; depends on #555 | Disable the switch or the single source; revert the pull request that set `JOB_PIPELINE_ENABLED` (not the master flag). Listings and matches are kept. |
| `publication` | `PUBLICATION_CONSUMER_ENABLED` (not present in the checked-in ConfigMap) | `publication_consumer` | none in the repository (`publication_sweep` command; see `docs/publication-outbox.md`) | Disable the switch; revert the enablement PR. Outbox rows are kept. |
| `match_recompute` | `MATCH_RECOMPUTE_ENABLED`, `MATCH_RESULTS_READ_ENABLED` (the scheduled command needs the master flag `AGENT_RUN_ENABLED`) | `match_recompute` (read side: `match_results_read`) | `deploy/cronjob-match-recompute.yaml` (`crank-match-recompute`) — created by no deploy; depends on #555 | Disable the switch; revert the enablement PR (order in `docs/match-recompute.md`). |
| `organization_crawl` | `CRAWL_CRON_ENABLED` (needs the master flag `AGENT_RUN_ENABLED`) | `crawl_schedule` | `k8s/crank-crawl-cron.yaml` (`crank-crawl-organizations`) | Disable the switch; revert the enablement PR. Accepted evidence is kept. |
| `score_source` | `GATHER_SCORES_ENABLED` (needs the master flag `AGENT_RUN_ENABLED`) | `gather_scores` | `k8s/cron-gather-scores.yml` (`crank-gather-scores`) | As in "Capability: Score Source (Gather Scores)" above. |

#### Shared master flag

`AGENT_RUN_ENABLED` is not a phase. It gates every scheduled command
(`run_job_pipeline`, `recompute_matches`, `schedule_crawls`, `gather_scores`;
`crank/management/base.py`), so four phases share it and the phases are not
independently reversible at the settings level unless it is handled
separately:

| Step | Rule |
|---|---|
| Enable | `AGENT_RUN_ENABLED` is set to `"true"` in its own pull request, before the `job_source` pull request. On its own it starts no work: each command also needs its own flag and its switch. |
| Roll back one phase | Disable that phase's switch, then revert only that phase's flag. Leave `AGENT_RUN_ENABLED` alone. |
| Revert the master flag | Last, and only when `JOB_PIPELINE_ENABLED`, `CRAWL_CRON_ENABLED`, `GATHER_SCORES_ENABLED` and `MATCH_RECOMPUTE_ENABLED` are all `"false"` again. |

Reverting the master flag while `JOB_PIPELINE_ENABLED` or `CRAWL_CRON_ENABLED`
is still true makes `GET /healthz/ready/` return 503 (the capability report
treats either flag without the master flag as misconfigured), so new web pods
fail their readiness probe; and every other scheduled command stops doing
work without any signal of its own.

**The shell has no switch.** It has been part of every deployed image since it
merged, because every merge to `main` deploys, so it cannot be staged and its
only rollback is redeploying the previous image. Its decision row therefore
uses evidence gathered before and after release rather than a switch.

#### The shell's rollback is not durable

**Redeploying the previous image is undone by the next merge to `main`.**
Neither deploy workflow can be started by hand, so the previous image is put
back by a change made in the cluster, and both workflows then overwrite it:

- a merge that changes code builds a new image from `main`, which still
  contains the change being rolled back, and deploys it
  (`deploy-home.yml`);
- a merge that changes only `k8s/` — every enablement and revert pull
  request in the "Durable enablement rule" — re-applies `k8s/crank.yml` with
  the image tag `latest` (`update-home-deployment.yml`), and `latest` is the
  newest build, the one that was rolled back from.

To make a shell rollback stick, the operator does two things, in this order:

1. Redeploy the previous image tag, to stop the harm now.
2. Merge a revert commit to `main` that removes the change, and wait for
   that merge to deploy. From then on the newest build no longer contains
   the change. **Merge no other pull request between step 1 and the deploy
   of that revert**: any other merge puts the rolled-back build back.

This describes the workflows as they are; nothing here changes them.

### Decision gates per phase

Gate names are the `release_gates:` entries in `docs/monitoring.yaml`; the
queries live there and tests bind them to the telemetry allowlists. A gate is
`provisional` (it has a number; "Provisional numbers for #492 gates" below
says which were carried over and which are new) or `baseline_required` (no
number until 14 days of data exist with the capability enabled). The outcome
rules are in `docs/monitoring.md`, "Release decision gates"; in short:

- **No gate can pass today.** Every gate's sample floor (`min_sample`) is
  unset, and an unset floor means **hold — insufficient data** whatever the
  query returns. The floors are the owner's to lock; they are listed with the
  other
  [decisions pending owner confirmation](usability-validation.md#decisions-pending-owner-confirmation).
- With a floor set, a sample below it is still **hold**, never a pass.
- An `alerts_quiet` gate needs two more things before "no alert opened" counts
  as evidence: its signal event must be arriving (the floor counts that event
  over the window), and the owner must have confirmed that the named alerts
  exist in the alerting tool. The repository defines the alert queries in
  `docs/monitoring.yaml` but nothing in it creates the alert policy, so this
  is a manual check, recorded in the decision record. "Exist" means the
  alert's query, threshold and window equal its entry in
  `docs/monitoring.yaml` and it has been seen to open once; a same-named
  alert with another threshold does not satisfy it.

- A gate is evaluated only over a **clean window**: one that lies wholly
  after the phase's post-merge check succeeded, with the switch on and no
  source disabled throughout. A held gate blocks expansion.

Entry criteria that the repository does not yet satisfy:

- **Every phase after `shell`: [#555](https://github.com/norcalipa/crank/issues/555)
  (open).** A hard precondition. Until deploys stop blanking
  `crank-capability-secrets`, no credentialed capability can stay enabled,
  and enabling `interactive_replies` by commit takes the web pods out of
  readiness (see "Durable enablement rule"). The same issue covers creating
  the `crank-job-pipeline` and `crank-match-recompute` CronJobs, without
  which `job_source` and `match_recompute` have no scheduled run.

- **Gates reading `inventory_health` or `pipeline_health`**
  (`job-source-alerts-quiet`, `publication-outbox-age`,
  `evidence-stale-share`): only `crawl_healthcheck` emits those events, and
  its CronJob is `suspend: true` in `k8s/crank-healthcheck-cron.yaml`. Until
  that line is committed as `false`, these gates have no sample and hold.
- **`interactive_replies`: a spend guard.** The checked-in
  `k8s/crank-agent-config.yml` sets `LLM_PRICE_PER_1K_TOKENS_USD` and
  `LLM_PER_USER_COST_LIMIT_USD` to `"0"`. With a price of `0` every call
  records `estimated_cost_usd: 0.0`, so the `cost-limit` alert that
  `interactive-alerts-quiet` names can never open, and a per-user ceiling of
  `0` turns that ceiling off (`crank/agents/llm.py`). The enablement pull
  request for this phase sets a real price and a ceiling the owner chose;
  until then "quiet" says nothing about cost and the phase has no spend
  guard.
- **`job_source`: an alert that is not a gate input.** `rejection-spike` is
  not named by `job-source-alerts-quiet`: it sums `items_failed`, which the
  job pipeline's `source_stage` events do not carry, so it cannot open in
  this phase.
- **`match_recompute`: an alert to try.** `deadline-resource-pressure` sums
  `deadline_reached`, which is sent as a boolean. Whether the alerting tool
  sums it to a non-zero number is not tested here; the owner's "seen to open
  once" check settles it (see the decision in
  `docs/usability-validation.md`).
- **`publication` phase**: nothing schedules the drain. `publication_sweep`
  is the only consumer of the outbox and no CronJob in `k8s/` or `deploy/`
  runs it, so setting `PUBLICATION_CONSUMER_ENABLED` alone leaves the outbox
  undrained and `publication-outbox-age` can only watch it grow. A scheduled
  sweep (a separate change; this section adds none) is a prerequisite of the
  phase's enablement pull request.

| Phase id | Gates | Window | Decision |
|---|---|---|---|
| `shell` | Playwright Django workflow green on the release SHA; moderated round passes A1 and A2 (`docs/usability-validation.md`); manual accessibility evidence recorded (`docs/e2e-validation.md`, "Manual evidence pending"); `priorities-apply-success` | 14 days | expand / hold / roll back |
| `interactive_replies` | `interactive-reply-success`, `interactive-alerts-quiet`, `interactive-time-to-first-result` | 7 days (14 days for time to first result) | expand / hold / roll back |
| `job_source` | `job-source-alerts-quiet`, `job-matches-source-unavailable`, `assistant-ready-share`; `release_verdict` of the readiness record has no blockers | 7 days | expand / hold / roll back |
| `publication` | `publication-outbox-age` (prerequisite: a scheduled `publication_sweep`, see above) | 14 days observed; the gate reads the latest 24 hours | expand / hold / roll back |
| `match_recompute` | `publication-to-match-lag`, `matching-alerts-quiet` | 14 days (7 days for `matching-alerts-quiet`) | expand / hold / roll back |
| `organization_crawl` | `evidence-stale-share` | 14 days observed; the gate reads the latest 24 hours | expand / hold / roll back |
| `score_source` | no telemetry gate; the existing Score Source stage tables above apply unchanged | per stage table | per stage table |

`assistant-ready-share` belongs to `job_source`, not to
`interactive_replies`: the assistant reports `ready` only when replies are on
**and** an active listing exists under an approved, enabled source
(`crank/views/assistant_status.py`). With replies on and no inventory every
signed-in poll is `inventory_unavailable`, so the share would be 0 for the
whole `interactive_replies` window by construction. The gate therefore also
presupposes that `interactive_replies` is still enabled, which the phase
order guarantees.

### Provisional numbers for #492 gates

Every number a `provisional` gate carries is listed here, with whether it was
already in this document or is **new in #492**. No other gate has a number.
Only one number is carried over; the other four gates' numbers were written
for #492 and have not been measured or approved.

| Gate | Number | Carried over or new | Came from |
|---|---|---|---|
| `interactive-reply-success` | `0.90`, breach when below, over 7 days | carried over | "Capability: Interactive Agent", "Stage 3: Limited Production": "90%+ success rate over window", observation window 7 days |
| `job-matches-source-unavailable` | `0`, breach when the share of `job_matches` availability states that are `no_source` or `source_disabled` is above it, over 7 days | new in #492 | Derived from issue #492's acceptance criterion "Release promises of live jobs require functioning inventory/matching": a phase described as live jobs may not serve a source-unavailable state. Only `no_source` and `source_disabled` count as unavailable. `crawl_failed`, `crawl_stale` and `crawl_empty` (sources enabled, nothing fresh to show) count toward the good share on purpose: `job-source-alerts-quiet` covers them through `stale-inventory`, `repeated-failures` and `zero-active-listings`. Not a measured value. |
| `interactive-alerts-quiet`, `job-source-alerts-quiet`, `matching-alerts-quiet` | `0` named alerts open at any time in the window | new in #492 | The per-alert thresholds under `alerts:` in `docs/monitoring.yaml` already existed and are unchanged. Requiring that none of them was open at any time in a whole window is a bar this section adds. `no-recent-success` is deliberately **not** in `job-source-alerts-quiet`: it opens when no `scheduled_run` succeeded in 6 hours, the only scheduled run in that phase is `crank-job-pipeline` on a 6-hour schedule, and the event is sent when a run finishes, so the alert would open whenever one run finished later into its slot than the one before. A bar of `0` would be unreachable. |

### Decision record template

One record per phase decision, written in the enablement (or revert) pull
request body (public; see decision D7). Until a record is filled in for a specific release SHA, this is
the *record format*, not a decision.

| Field | Value |
|---|---|
| Phase id | _one of the phase ids above_ |
| Decision | _expand / hold / roll back_ |
| Enablement commit | _merge commit of the enablement PR (no image is built from it)_ |
| Release SHA | _`source_version` of the readiness record: the image the pods are running, which is the last image build on `main`, not the enablement commit_ |
| Readiness record | _`python manage.py readiness_baseline --out <file>`: `source_version`, `fixtures.revision`, `release_verdict`_ |
| Source / fixture readiness | _`source_counts`, `inventory.violations`, `listing_counts.active_from_live_sources`; fixtures must be absent for a production decision_ |
| Post-merge check | _time of the HTTP 200 from `/healthz/ready/` with the capability `enabled: true` and `ok: true`; gate windows start here_ |
| Gate results | _per gate: window start and end, observed value, sample (`sample_nrql` result) against the locked floor, the step that decided, pass / breach / hold_ |
| Alert policy check | _for each `alerts_quiet` gate: who confirmed each named alert exists in the alerting tool with the query, threshold and window of `docs/monitoring.yaml` and has been seen to open once, and when_ |
| Failures | _what failed or was assisted, with issue numbers_ |
| Follow-up fixes | _issue or PR numbers, each fixed or explicitly accepted_ |
| Rollback evidence | _`rollback_drill --json` result; `data_counts` from records taken before and after, compared_ |
| Sign-off | _as decision D5 defines: GitHub handle, role signed for, date_ |

## Evidence Storage

- **Run records:** `AgentRun` (status, counts, error_summary, correlation_id)
- **Source records:** `SourceRun` (per-source status, counts)
- **Change audit:** `OperationalChangeAudit` (actor, action, old/new, confirmed)
- **Monitoring events:** New Relic custom events (CrankOperation)
- **Dashboard links:** New Relic dashboards (see `docs/monitoring.md`)

No secrets, credentials, prompts, or user data are stored in rollout records.
The `_safe_value` redaction in `OperationalChangeAudit` enforces this at the
model boundary.

## Non-Blocking Findings

Findings that do not block rollout are tracked as follow-up GitHub issues.
Examples: minor latency optimization opportunities, dashboard layout
improvements, additional alerting thresholds.

## Security and Observability

- Use approved change control and least-privilege access for all flag
  changes.
- Do not place environment secrets or user data in rollout records.
- Critical security/privacy findings immediately trigger rollback.
- All flag changes go through admin actions that require an explicit
  confirmation page first (the confirm step re-POSTs a hidden
  `confirm=yes`); unconfirmed POSTs stay no-ops.

---

# Job Recommendation GA — Final Release Gate (#410)

<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

**Issue:** [#410](https://github.com/norcalipa/crank/issues/410)
**Phase:** 4 — Hardening and Rollout (General Availability closeout)
**Status:** Operational checklist — NOT a claim of production verification.

This section is the **final release gate** for production job retrieval and
chat UX. It is a closeout of the umbrella plan and does **not** re-implement
any child work. Each child issue below shipped through its own PR and is
already merged; this gate only documents how an operator verifies the
released surfaces before (and after) going live. **This document records the
expected procedure — it does not itself assert that production has been
verified.** Any production verification must be recorded in the
[Production verification record](#production-verification-record) below, by a
human operator.

## Child Issues, PRs, and Merge SHAs

All children are **closed and merged** ahead of this gate:

| Issue | PR | Merge SHAs | What shipped |
|-------|-----|------------|--------------|
| [#403](https://github.com/norcalipa/crank/issues/403) | [#412](https://github.com/norcalipa/crank/pull/412) | `6426df4` (+ follow-up `f9e685c`) | Release diagnostics, build-fingerprint drift alerting, migration health |
| [#404](https://github.com/norcalipa/crank/issues/404) | [#413](https://github.com/norcalipa/crank/pull/413) | `b6899d0` | Job Retrieval Operations admin surface |
| [#405](https://github.com/norcalipa/crank/issues/405) | [#416](https://github.com/norcalipa/crank/pull/416) | `e45f2dd` (+ follow-up `22f0544`) | Job source catalog and health monitoring |
| [#406](https://github.com/norcalipa/crank/issues/406) | [#417](https://github.com/norcalipa/crank/pull/417) | `f8c183c` | Chat recommendation history + reload |
| [#407](https://github.com/norcalipa/crank/issues/407) | [#414](https://github.com/norcalipa/crank/pull/414) | `56d0a0e` | Chat composer UX |
| [#408](https://github.com/norcalipa/crank/issues/408) | [#415](https://github.com/norcalipa/crank/pull/415) | `237f867` (+ follow-up `a8d50a9`) | Viewport-reactive shell |
| [#409](https://github.com/norcalipa/crank/issues/409) | [#411](https://github.com/norcalipa/crank/pull/411) | `0e1eaa6` | Organization list scrollbar |

No child work is re-implemented by this gate.

## Operator Checklist (manual, run by an authorized operator)

The following steps require a human operator with appropriate access. They
are **not** automated and **not** run against production by this repository's
CI. Anything labeled **automated** below is still operator-invoked or
covered by `crank/tests/test_release_gate_smoke.py` in CI and refers to wiring
checks, never a live production claim.

### 1. Release, Assets, Migrations, and Config Diagnostics

Verify the deployed SHA and that schema/assets/configuration are coherent
before trusting job retrieval:

- **Release SHA:** confirm the running build matches the **deployed release
  SHA** — the merge commit of this closeout PR on `main` (the current `main`
  tip / the release tag or artifact actually deployed to the environment) —
  and that its ancestry contains *all* children. Do **not** validate against an
  intermediate child merge SHA: every SHA in the child table predates the full
  closeout change set, so a child SHA can certify a stale/incomplete
  deployment. The child table is **historical evidence only**. Verify via the
  release page, the build fingerprint in the release-diagnostics view, and a
  build-fingerprint / ancestry check against that release target.
- **Assets:** confirm the webpack/manifest fingerprint matches the deployed
  build (release-diagnostics reports `build.status`; `mismatch` means the
  frontend bundle and backend do not agree — resolve before proceeding).
- **Migrations:** run `python manage.py migration_status` and confirm
  `pending == 0` / status `clean`. Be precise about its scope: `migration_status`
  verifies applied migrations plus the presence of **only** the two conversation
  tables it defines — `crank_jobsearchconversation` and
  `crank_jobsearchmessage`. It does **not** inspect job-source, listing, or
  crawl-run tables, so `status=clean` does **not** imply the retrieval schema is
  present. For the full migration state this gate depends on, run
  `python manage.py showmigrations --plan` (or the release-diagnostics view) and
  confirm the retrieval tables exist via `crawl_status` connectivity and the Job
  Retrieval Ops admin surface in
  [2. Admin Job Retrieval Ops](#2-admin-job-retrieval-ops--seed-and-listing-counts).
  **Automated in CI** only at the wiring level (see the smoke test); the live DB
  check here is operator-run.
- **Provider mode (production/staging gate FAILS on demo):**
  `JOB_SEARCH_PROVIDER` **must be `orchestrator`** (the real provider) for any
  production or staging gate. If it is `demo` — or any canned response — in a
  non-dev environment, the gate **FAILS** immediately; demo behavior is
  explicitly **non-production only** and contradicts #406's requirement that
  production use the real orchestrator and not display canned recommendations.
  Confirm provider/model readiness and that at least one real active
  listing/result card is produced before passing. List all relevant kill-switch
  defaults at runtime (see
  [Rollback + kill switches](#5-rollback-and-kill-switches)).

### 2. Admin Job Retrieval Ops — Seed and Listing Counts

- **Seed:** run `python manage.py seed_job_sources` (use `--dry-run` first to
  review) to create/update the approved+enabled `JobSourceCatalog` set.
  Idempotent; only `APPROVED_JOB_SOURCE_DOMAINS` allowlisted domains are
  seeded.
- **Dashboard:** open the **Job Retrieval Operations** admin page
  (`/admin/.../jobretrievalops/`, staff-only) and confirm sources, readiness,
  and audit actions load. **Automated (wiring only):** the smoke test asserts
  the admin surface is registered.
- **Run retrieval (required):** seeding sources alone produces **zero**
  inventory — the operator **must trigger retrieval** before any listing-count
  assertion. With readiness and budget gates confirmed, invoke the pipeline
  (`python manage.py run_job_pipeline`) and/or dispatch bounded crawls
  (`python manage.py schedule_crawls`, or a single confirmed crawl via
  `python manage.py trigger_crawl --source-key ... --source-type job --confirm`,
  or the dashboard's own run control), then wait for the run to complete.
- **Listing counts:** *after* a completed retrieval run, run
  `python manage.py crawl_status` and confirm per-source listing counts,
  last-crawl time, and last outcome are populated as expected. A non-zero
  listing count requires an executed run, not merely a seeded source.
- **Health:** run `python manage.py crawl_healthcheck` and confirm it emits a
  bounded telemetry event and reports no inventory anomalies.

### 3. Chat Recommendation End-to-End + History Reload

Child [#406](https://github.com/norcalipa/crank/issues/406) added conversation
history retention and reload; verify the loop as an operator:

- Open the chat (`/chat/`, the `job_search` page) as a logged-in user.
- Submit a message and confirm a **real** job recommendation is returned from
  live inventory. A `demo`/canned response is **not** acceptable for the
  production gate — `JOB_SEARCH_PROVIDER` must be `orchestrator` (see
  [Provider mode](#1-release-assets-migrations-and-config-diagnostics) above).
- Reload the page and confirm prior conversation history is restored via the
  retained-history endpoint.
- Exercise the conversation controls: list/detail, export (JSON, only the
  user's own fields), reset (fresh history), and delete.
- Confirm job-match list/detail/seen/dismiss/ranked/status endpoints respond.
  **Automated (wiring only):** the smoke test confirms the `job_search` URL
  resolves and the chat view is present.

### 4. UX Acceptance Notes

> **Automated viewport suite (formerly a permanent AC waiver):** the #407/#408
> viewport, zoom, keyboard, and IME acceptance criteria are now asserted by an
> automated Playwright harness ([#431](https://github.com/norcalipa/crank/issues/431))
> that renders the job-search chat and organization list across a desktop,
> tablet, mobile, and short-height viewport matrix (plus a Chromium 200%-zoom
> check and IME-composition guard) in Chromium, Firefox, and WebKit via the
> `Viewport E2E` workflow. The jest/jsdom suite still asserts layout wiring.
> The per-case evidence matrix below remains the operator-recorded record for a
> specific production release SHA.

- **#407 Composer:** the chat composer renders correctly, handles multi-line
  input, submit-on-Enter affordance, and empty/invalid send without error.
- **#408 Viewport shell:** verify across the viewport matrix required by
  #407/#408 — desktop (≥1280px), tablet (~768px), mobile (~375px), a
  short-height window (<700px), and 200% browser zoom — confirming no
  horizontal overflow, no hidden controls, and graceful panel collapse in every
  case. Re-check with the on-screen/virtual keyboard open and on
  `visualViewport` resize (mobile), and run the composer through an IME
  composition (e.g. CJK) to ensure in-progress input never jumps or is clipped.
  Record per-case evidence (viewport size, zoom level, keyboard state) for the
  [production record](#6-production-verification-record).
- **#409 Organization-list scrollbar:** the organization list shows **no**
  inner vertical scrollbar when the paginated rows fit — the document owns
  scrolling. A nested/independent scrollbar is only permitted where content
  intentionally exceeds its container (the detail dialog's bounded overflow);
  otherwise no inner scrollbar may appear at desktop and mobile sizes.

### 5. Rollback and Kill Switches

Pre-verified rollback path before enabling anything:

- Rehearse with `python manage.py rollback_drill` (optionally `--json`), and
  note its **precise scope**: it disables each capability `CapabilitySwitch`,
  verifies `capability_enabled()` returns `False` for the switch key (and for
  the matching run type where the key and run type align), checks for orphaned
  RUNNING runs beyond the stale-lock TTL, records an `OperationalChangeAudit`
  entry, and emits a monitoring event. It does **not** snapshot or assert
  `AgentRun` creation counts and does **not** call
  `AgentRunCommand.get_enabled()`; for `interactive_agent` (where the drilled
  `noop` run type mismatches the switch key) the settings flags are the primary
  gate and the switch is an additional defense.
- Confirm new-run blocking (covers the guarantees `rollback_drill` does not):
  with the kill switch and env flags disabled, call
  `AgentRunCommand.get_enabled()` for the drilled run types and confirm it
  returns `False`, and confirm no new `AgentRun` row is created when a run is
  attempted (compare `AgentRun` counts before/after).
- Confirm the runtime kill switch `CapabilitySwitch(key="job_pipeline")` (and
  `interactive_agent` / `gather_scores` where applicable) blocks `get_enabled()`.
- Confirm environment flags: `AGENT_RUN_ENABLED` (master, default `False`),
  `AGENT_NOOP_ENABLED`, `JOB_PIPELINE_ENABLED` (default `False`),
  `CRAWL_CRON_ENABLED` (default `False`) gate their respective pipelines.
- **Automated (wiring only):** the smoke test confirms `rollback_drill` is a
  registered command and importable with the expected interface.

### 6. Production Verification Record

Any claim that production is verified **must** be recorded here by a human
operator. Until this table is filled in for a specific release SHA, this
section documents the *record format*, not a verified state:

| Field | Value |
|---|---|
| Release SHA | _record the deployed commit SHA_ |
| Source count | _count from `crawl_status` / admin dashboard_ |
| Listing count | _total active listings from `crawl_status`_ |
| Provider mode | `demo` or provider name (from `JOB_SEARCH_PROVIDER`)_ |
| Latest successful run | _date/outcome of last `run_job_pipeline` / crawl_ |
| Desktop screenshot | _attach file / link_ |
| Mobile screenshot | _attach file / link_ |
| UX evidence matrix | _viewport / zoom / keyboard / IME per case (see #408)_ |

## Remaining Operator Evidence (blocks #410 close)

This PR intentionally does **not** auto-close **#410** via `Fixes`; it is linked
with **Refs / Contributes to**. The code and documentation land here, but the
umbrella issue stays open until an operator records the following
environment-dependent evidence **here or on #410** for a specific release SHA:

- [ ] **Authenticated staging smoke:** seed → `run_job_pipeline` / scheduled
      crawl → non-zero listings → chat recommendation from real inventory →
      history reload → follow-through (list/detail/export/reset/delete/
      seen/dismiss).
- [ ] **Production verification record** filled in under
      [6. Production Verification Record](#6-production-verification-record)
      for a release SHA, with `JOB_SEARCH_PROVIDER=orchestrator` (**no demo**)
      and at least one real active listing/result card.
- [ ] **Rollback and kill switches** exercised, including `get_enabled()`
      returning False and no new `AgentRun` rows (see
      [5. Rollback and Kill Switches](#5-rollback-and-kill-switches)).
- [ ] **UX evidence** for the viewport/zoom/keyboard/IME matrix (see
      [4. UX Acceptance Notes](#4-ux-acceptance-notes)).

**#403 post-deploy smoke AC — documented split (accepted gap):** the smoke
acceptance is covered by the automated CI wiring test
(`test_release_gate_smoke.py` — commands registered, key URLs resolve) plus
this **operator-run authenticated smoke** (seed → `run_job_pipeline` / crawl →
non-zero listings → chat recommendation → history reload → follow-through).
There is **no operator-automated hook** (e.g. a scheduled job executing an
authenticated end-to-end smoke against a deployed environment) at this gate;
that is a documented, accepted gap rather than a silent miss. Deployment health
that *is* automated in CI/ops is covered by `migration_status` and the
`release-diagnostics` build-fingerprint drift alerting (`release_build_status`).

Closing #410 is an **operator action** backed by the evidence above, not a
merge side-effect.

## Summary: Automated vs. Operator-Run

| Item | Automated (CI smoke) | Operator-run (this gate) |
|---|---|---|
| Management commands registered | `test_release_gate_smoke.py` | — |
| Key URLs resolve | `test_release_gate_smoke.py` | live env resolution |
| Job Retrieval Ops admin registered | `test_release_gate_smoke.py` | open the dashboard |
| `rollback_drill` importable/interface | `test_release_gate_smoke.py` | `rollback_drill` run |
| Migration graph on test DB | existing smoke suite | `migration_status` on live DB |
| Source seed / listing counts | — | `seed_job_sources`, `crawl_status`, dashboard |
| Crawl health | — | `crawl_healthcheck` |
| Chat E2E + history reload | — | manual chat walkthrough |
| UX acceptance (composer/shell/scrollbar) | — | manual viewport/zoom/keyboard/IME matrix review |
| Rollback rehearsal | — | `rollback_drill` |
| Kill-switch confirmation | — | env + `CapabilitySwitch` checks |
| Production verification record | — | filled by operator per release |

This gate **does not** claim production was verified. CI verifies code wiring;
production verification requires the operator checklist and the completed
record above.
