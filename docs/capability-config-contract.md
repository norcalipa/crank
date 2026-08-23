<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Production Capability Configuration Contract

**Issue:** [#440](https://github.com/norcalipa/crank/issues/440)
**Phase:** 4 — Hardening and Rollout
**Status:** Active

## Purpose

This document defines the single, documented, environment-backed production
capability contract shared across the web Deployment, migration Job, and
scheduled agent Jobs. It ensures that every workload consumes the same
versioned configuration, that enabled capabilities have their required
secrets, and that misconfigured capabilities fail before serving traffic.

## Capability Contract

### Config Version

The capability contract has a `CONFIG_VERSION` (currently `"1"`) defined in
`crank/capability.py`. Bump this when the contract structure changes so
diagnostics can detect manifest/config version drift.

### Capabilities

| Capability | Master flag | Required settings | Required secrets |
|---|---|---|---|
| Interactive Agent | `INTERACTIVE_AGENT_ENABLED` | `LLM_PROVIDER`, `LLM_MODEL` | `LLM_API_KEY` |
| Job Pipeline | `JOB_PIPELINE_ENABLED` | `AGENT_RUN_ENABLED=true` | Per-source credentials |
| Crawl Scheduling | `CRAWL_CRON_ENABLED` | `AGENT_RUN_ENABLED=true` | Per-source credentials |

### Non-secret ConfigMap (`crank-agent-config`)

All non-secret capability knobs live in the `crank-agent-config` ConfigMap:
feature flags, provider/model names, timeouts, limits, and schedules.

### Secret Reference (`crank-capability-secrets`)

All capability credentials live in the `crank-capability-secrets` Kubernetes
Secret. The manifest (`k8s/crank-capability-secrets.yml`) defines the
structure with empty values; operators populate real values out-of-band:

```sh
kubectl -n crank create secret generic crank-capability-secrets \
  --from-literal=LLM_API_KEY='sk-...' \
  --from-literal=YELP_API_KEY='...' \
  --from-literal=USAJOBS_AUTH_KEY='...' \
  --from-literal=USAJOBS_USER_AGENT_EMAIL='...' \
  --from-literal=FIRECRAWL_API_KEY='...'
```

The deploy workflow uses `--dry-run=client` so applying the manifest never
overwrites values an operator has already set.

## Workload Wiring

| Workload | ConfigMaps | Secrets |
|---|---|---|
| Web Deployment | `crank-config`, `crank-agent-config` | `db-connect-credentials`, `crank-capability-secrets` |
| Migration Job | `crank-config`, `crank-agent-config` | `db-connect-credentials`, `crank-capability-secrets` |
| Agent No-op CronJob | `crank-config`, `crank-agent-config` | `db-connect-credentials`, `crank-capability-secrets` |
| Crawl Organizations CronJob | `crank-config`, `crank-agent-config` | `db-connect-credentials`, `crank-capability-secrets` |
| Crawl Jobs CronJob | `crank-config`, `crank-agent-config` | `db-connect-credentials`, `crank-capability-secrets` |
| Healthcheck CronJob | `crank-config`, `crank-agent-config` | `db-connect-credentials`, `crank-capability-secrets` |
| Gather Scores CronJob | `crank-config`, `crank-agent-config` | `db-connect-credentials`, `crank-capability-secrets` |

## Deploy Workflow

Both `deploy-home.yml` and `update-home-deployment.yml` apply manifests in
deterministic order:

1. Namespace
2. ConfigMaps (`crank-config`, `crank-agent-config`)
3. Secrets (`db-connect-credentials` out-of-band, `crank-capability-secrets` via dry-run)
4. Migration Job (wait for completion)
5. CronJobs (agent-noop, crawl-organizations, crawl-jobs, healthcheck, gather-scores)
6. Web Deployment

## Fail-Closed Readiness

The `/healthz/ready/` endpoint performs two checks:

1. **Migration check**: no unapplied migrations.
2. **Capability check**: every enabled capability has its required
   configuration. A disabled capability never affects readiness.

When an enabled capability is missing required config, readiness returns 503
so the pod does not receive traffic. The response includes safe booleans and
non-secret issue descriptions; secret values are never included.

## Staff Diagnostics

The staff-only `/staff/release-diagnostics/` page and the `diagnostics()`
function in `crank/release.py` report:

- `interactive_agent_enabled`: bool
- `llm_configured`: bool (provider is set)
- `llm_model`: safe token string
- `llm_api_key_present`: bool (never the key value)
- `agent_run_enabled`: bool
- `job_pipeline_enabled`: bool
- `crawl_scheduling_enabled`: bool
- `capability_config_version`: string
- `capability_all_ok`: bool
- `capability_issues`: list of non-secret issue strings

## Rollback and Kill-Switch

### Independent Kill Switches

Each capability can be disabled independently without affecting others:

| To disable | Set flag to `false` | Effect |
|---|---|---|
| Interactive chat | `INTERACTIVE_AGENT_ENABLED=false` | Chat page shows disabled state; no LLM calls |
| Job pipeline | `JOB_PIPELINE_ENABLED=false` | Scheduled job ingestion stops |
| Crawl scheduling | `CRAWL_CRON_ENABLED=false` | Crawl CronJobs do no work |
| All scheduled work | `AGENT_RUN_ENABLED=false` | All CronJobs exit 0 without claiming work |

### Runtime Kill Switch (DB-backed)

`CapabilitySwitch` records in the database provide a runtime kill switch
that does not require a redeploy:

```python
from crank.models.monitoring import CapabilitySwitch
# Disable interactive agent at runtime
CapabilitySwitch.objects.update_or_create(
    key="interactive_agent",
    defaults={"enabled": False, "note": "Emergency disable via kill switch"},
)
```

### Rollback Procedure

1. **Config rollback**: revert the `crank-agent-config` ConfigMap to the
   previous version (e.g., `kubectl rollout undo deployment/crank` or
   re-apply the previous ConfigMap).
2. **Code rollback**: redeploy the previous image tag
   (`ghcr.io/norcalipa/crank/crank:<previous-sha>`).
3. **Kill switch**: if a full rollback is not needed, disable the specific
   capability via the ConfigMap flag or DB `CapabilitySwitch`.
4. **Verify**: check `/healthz/ready/` and `/staff/release-diagnostics/`
   to confirm the capability is disabled and readiness is green.

### Coherent Code/Config/Manifest Set

The deploy workflow applies ConfigMaps, Secrets, and manifests atomically
before the web Deployment. Rolling back the image tag alone does not roll
back ConfigMaps; operators must re-apply the previous ConfigMap version
explicitly. The `capability_config_version` in diagnostics helps confirm
which contract version is active.
