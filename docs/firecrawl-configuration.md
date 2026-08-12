<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Firecrawl Careers Adapter Configuration

## Overview

The `firecrawl-careers` adapter wraps the [Firecrawl](https://firecrawl.dev) provider behind CRank's existing `JobSourceAdapter` contract. It issues a bounded structured-extraction crawl to an approved company career site and normalizes the results into `RawJobListing` values for the existing `ingest_jobs` / `JobListing.upsert_from_raw` pipeline.

The adapter is **disabled by default**. Enabling it requires:

1. A code-approved career-site domain in `APPROVED_JOB_SOURCE_DOMAINS`.
2. An operator-approved, enabled `JobSourceCatalog` row with `adapter_key="firecrawl-careers"`.
3. A Firecrawl API key in the secret store or environment.

## Settings

| Setting | Env var | Default | Purpose |
| --- | --- | --- | --- |
| `FIRECRAWL_ENABLED` | `FIRECRAWL_ENABLED` | `False` | Master switch; must be truthy for the adapter to start. |
| `FIRECRAWL_API_KEY` | `FIRECRAWL_API_KEY` | `""` | Provider API key; read from environment only, never logged. |
| `FIRECRAWL_BASE_URL` | `FIRECRAWL_BASE_URL` | `https://api.firecrawl.dev` | Provider base URL. Override for self-hosted deployments. |
| `FIRECRAWL_TIMEOUT` | `FIRECRAWL_TIMEOUT` | `30.0` | Connect/read timeout in seconds. |
| `FIRECRAWL_MAX_PAGES` | `FIRECRAWL_MAX_PAGES` | `10` | Maximum pages per crawl. |
| `FIRECRAWL_MAX_LISTINGS` | `FIRECRAWL_MAX_LISTINGS` | `100` | Maximum listings per crawl. |
| `FIRECRAWL_CREDIT_BUDGET` | `FIRECRAWL_CREDIT_BUDGET` | `10` | Per-run credit/request budget. When zero, no external call is made. |

## Hosted Firecrawl

The default base URL is `https://api.firecrawl.dev`. Set `FIRECRAWL_API_KEY` in the environment and enable the adapter:

```bash
export FIRECRAWL_API_KEY="fc-..."
export FIRECRAWL_ENABLED=true
```

## Self-hosted Firecrawl

Firecrawl can be self-hosted. Point `FIRECRAWL_BASE_URL` at your instance:

```bash
export FIRECRAWL_BASE_URL="https://firecrawl.internal.example.com"
export FIRECRAWL_API_KEY="fc-..."
export FIRECRAWL_ENABLED=true
```

The base URL must be HTTPS without credentials in the URL.

## Security and cost controls

- **HTTPS only:** the provider base URL and every career-site URL must use HTTPS.
- **Code-owned allowlist:** career-site domains are approved in source code (`APPROVED_JOB_SOURCE_DOMAINS`). Database rows cannot expand the network allowlist.
- **Bounded provenance:** `source_metadata` stores only `source_url`, `crawl_job_id`, `extraction_version`, and `observed_at`. Raw HTML/markdown and credentials are never persisted.
- **Versioned extraction schema:** the structured-extraction schema is defined in code (`EXTRACTION_SCHEMA`) and cannot be modified by catalog rows.
- **Idempotent upserts:** replaying the same crawl updates freshness without duplicating rows. Partial crawls do not close unseen jobs.
- **Credit budget:** when `FIRECRAWL_CREDIT_BUDGET` is zero, the adapter refuses to make any external call.
- **Telemetry:** emits `firecrawl_requests_total`, `firecrawl_credits_total`, and `firecrawl_errors_total` counters with no sensitive payloads.
