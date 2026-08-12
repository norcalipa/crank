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
   (requires `confirm=yes`).
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
- All flag changes go through admin actions requiring `confirm=yes`.
