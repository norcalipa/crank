<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Operator Runbook: Provider and Source Configuration

**Owner:** CRank maintainers
**Last reviewed:** 2026-08-12
**Version/change process:** Changes follow the [architecture documentation](./readme.md) and must pass CI gates.

## Purpose

Configure LLM providers and external data sources (rating sources, job sources) through environment-backed settings. No secrets are checked into source control.

## LLM Provider Configuration

| Setting | Environment variable | Default | Notes |
|---|---|---|---|
| Provider class | `LLM_PROVIDER` | `""` (empty) | Python dotted path to a provider implementation. `crank.agents.llm:FakeLLMProvider` is the safe offline default. |
| Model name | `LLM_MODEL` | `""` | Provider-specific model identifier. |
| API key | `LLM_API_KEY` | `""` | Read from environment secret only. Never committed. |
| Timeout | `LLM_TIMEOUT_SECONDS` | `30` | Wall-clock timeout per request. |
| Max tokens | `LLM_MAX_TOKENS` | `2048` | Maximum tokens per completion. |
| Per-user cost limit | `LLM_PER_USER_COST_LIMIT_USD` | `0` | Cumulative per-user spend ceiling (best-effort, per-process). |
| Price per 1K tokens | `LLM_PRICE_PER_1K_TOKENS_USD` | `0` | Used for cost-limit enforcement. |
| Interactive agent flag | `INTERACTIVE_AGENT_ENABLED` | `false` | Gates interactive agent independently of scheduled ingestion. |

### Setup

1. Set `LLM_PROVIDER` to the desired provider class (e.g., `crank.agents.llm:OpenAIProvider`).
2. Set `LLM_MODEL` to the provider-specific model name.
3. Set `LLM_API_KEY` as a Kubernetes secret, mounted as an environment variable.
4. Set `INTERACTIVE_AGENT_ENABLED=true` to enable the interactive agent.
5. Verify with a health check: `GET /healthz/ready/`.

### Disablement

Set `INTERACTIVE_AGENT_ENABLED=false` to disable the interactive agent without affecting scheduled ingestion.

## External Source Configuration

Rating and job sources are configured through the Django admin or database-backed `SourceCatalog` records. Each source records:

- Name, linked rating `Organization`, adapter key, base URL.
- Approval state (`pending`, `approved`, `blocked`), enabled flag.
- Cadence, timeout, rate limit, data retention policy.
- Supported score types or job data capabilities.

### Source approval

1. Create a `SourceCatalog` record in Django admin.
2. Set `approval_state` to `approved`.
3. Set `enabled` to `true`.
4. Verify the adapter key is registered in code (`crank.agents.sources`).

### Source disablement

1. Locate the source in Django admin.
2. Set `enabled` to `false` or `approval_state` to `blocked`.

## Feature/Source Flags

| Flag | Environment variable | Default |
|---|---|---|
| Agent runs | `AGENT_RUN_ENABLED` | `false` |
| No-op reference run | `AGENT_NOOP_ENABLED` | `false` |
| Score gathering | `GATHER_SCORES_ENABLED` | `false` |
| Job pipeline | `JOB_PIPELINE_ENABLED` | `false` |
| Interactive agent | `INTERACTIVE_AGENT_ENABLED` | `false` |

Each flag is independent. Setting `JOB_PIPELINE_ENABLED=false` does not affect score gathering.

## Scheduled Commands

Scheduled runs are Kubernetes `CronJob` resources that invoke Django management commands:

| Run type | Command | Default schedule |
|---|---|---|
| No-op reference | `python manage.py agent_noop` | Disabled by default |
| Score gathering | `python manage.py gather_scores` | Configured per source cadence |
| Job pipeline | `python manage.py job_pipeline` | Configured per deployment |

Each command is idempotent and safe under overlapping scheduler invocations. The database enforces at most one `running` row per run type.
