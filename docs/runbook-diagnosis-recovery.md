<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Operator Runbook: Run Diagnosis, Stale Locks, and Recovery

**Owner:** CRank maintainers
**Last reviewed:** 2026-08-12
**Version/change process:** Changes follow the [architecture documentation](./readme.md) and must pass CI gates.

## Run Diagnosis

### Inspect recent runs

1. Open Django admin → AgentRun records.
2. Filter by run type and status.
3. Check `started_at`, `finished_at`, and `error_summary` fields.
4. The `correlation_id` field links runs across logs and New Relic events.

### Query via shell

```sh
kubectl exec deployment/crank -- python manage.py shell
```

```python
from crank.models.agent_run import AgentRun
runs = AgentRun.objects.filter(status="failed").order_by("-created")[:10]
for r in runs:
    print(r.correlation_id, r.run_type, r.status, r.error_summary)
```

### New Relic

Search for the `correlation_id` in New Relic logs and events to trace a run's full lifecycle.

## Stale Locks

A run in `running` status past the stale threshold (`AGENT_RUN_STALE_AFTER_SECONDS`, default 3600) is stale. The database enforces at most one `running` row per run type — a stale lock blocks new runs.

### Resolve a stale lock

```python
from crank.models.agent_run import AgentRun
from django.utils import timezone

stale = AgentRun.objects.filter(
    status="running",
    started_at__lt=timezone.now() - timezone.timedelta(seconds=3600)
)
for run in stale:
    run.status = "failed"
    run.error_summary = "Marked as stale by operator"
    run.finished_at = timezone.now()
    run.save()
```

After clearing the stale lock, the next scheduled run will proceed normally.

## Retries

Retries use bounded exponential backoff with jitter for transient failures only. A source that fails does not prevent approved independent sources from completing. Check `AgentRun` records and per-source run results for retry history.

## Alerts

Configure New Relic alerts for:
- Failed runs (status = `failed`).
- Stale locks (runs in `running` longer than `AGENT_RUN_STALE_AFTER_SECONDS`).
- Cost threshold approach (`LLM_PER_USER_COST_LIMIT_USD`).

## Cost Controls

| Control | Setting | Notes |
|---|---|---|
| Per-user cost limit | `LLM_PER_USER_COST_LIMIT_USD` | Cumulative per-process ceiling. |
| Price per 1K tokens | `LLM_PRICE_PER_1K_TOKENS_USD` | Used for cost calculation. |
| Max tokens | `LLM_MAX_TOKENS` | Per-request ceiling. |
| Timeout | `LLM_TIMEOUT_SECONDS` | Wall-clock per-request limit. |

Usage is returned as `LLMUsage` (`prompt/completion/total_tokens`, `cost_estimate_usd`) without storing prompt content in metrics.

## Source Approval

1. Verify API availability, license/terms, robots policy, authentication, rate limits, and retention limits.
2. Create a `SourceCatalog` record with `approval_state=pending`.
3. Complete review and set `approval_state=approved`.
4. Set `enabled=true` to activate.
5. Blocked sources (`approval_state=blocked`) must not run.

## Data Deletion

### Delete a user's data

```python
from django.contrib.auth.models import User
user = User.objects.get(username="<username>")
user.delete()  # Cascades to preferences, conversations, matches
```

### Delete specific conversation

```sh
DELETE /api/agent/conversations/<conversation_id>/delete/
```

## Rollback

### Code rollback

1. Identify the last known-good deployment:
   ```sh
   kubectl rollout history deployment/crank
   ```
2. Roll back:
   ```sh
   kubectl rollout undo deployment/crank
   ```

### Score rollback

Score observations preserve history. To revert to a previous observation:

```python
from crank.models.score import Score
# Deactivate current active score
Score.objects.filter(type_id=X, source_id=Y, target_id=Z, status=1).update(status=0)
# Reactivate previous observation
Score.objects.filter(id=<previous-id>).update(status=1)
```

After any score change, invalidate affected caches:
```python
from django.core.cache import cache
cache.clear()
```

## Versioning and Change Review

Model, prompt, ranker, and source changes follow these expectations:

| Component | Versioning | Change review |
|---|---|---|
| Preference schema | `SCHEMA_VERSION` integer | Migration maps old documents to new shape |
| LLM provider | `LLM_PROVIDER` + `LLM_MODEL` settings | Environment change, no code deployment needed |
| Source adapters | Adapter key in `SourceCatalog` | Code review + source approval required |
| Ranker | Ranking config in code | Code review + CI tests |
| Prompts | System prompt in code (`crank.agents.job_search.system_prompt`) | Code review + CI tests |

All changes must pass CI gates (tests, lint, coverage) before merge.
