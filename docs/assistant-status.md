<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Assistant availability status contract (issue #457)

`GET /api/agent/assistant-status/` is an **advisory, user-safe** availability
surface for the job-search assistant. It lets the chat UI avoid futile
submissions and offer useful actions instead.

## Contract

- `GET` only; login **not** required; CSRF-exempt (safe read-only GET).
- HTTP **200** for every state — the state is data, not an error.
- Bounded envelope, never extended with diagnostics:

  ```json
  {
    "state": "ready",
    "actions": ["browse_rankings"],
    "checked_at": "2026-09-14T18:00:00+00:00"
  }
  ```

- `actions` are opaque slugs; the client maps them to links/copy:
  - `browse_rankings` — a link to `/` (organization rankings) stays useful.
  - `retry` — re-checking the status is meaningful (transient conditions).

## States and precedence

Evaluated top-down; the first match wins:

| # | State | Meaning |
|---|-------|---------|
| 1 | `signed_out` | Anonymous requester (the chat requires login). |
| 2 | `replies_disabled` | `INTERACTIVE_AGENT_ENABLED` is false, **or** `JOB_SEARCH_PROVIDER` is `demo`/unset in a non-dev environment (`crank.checks.is_non_dev_environment()`). |
| 3 | `temporarily_unavailable` | Capability enabled but fail-closed provider construction raised `AssistantUnavailable` (e.g. missing LLM configuration at runtime). |
| 4 | `refreshing` | A `job_pipeline` `AgentRun` is PENDING/RUNNING; inventory may be mid-refresh. |
| 5 | `inventory_unavailable` | Zero active `JobListing` rows under approved+enabled `JobSourceCatalog` sources. |
| 6 | `ready` | Everything checked out; the send path is expected to work. |

## Safety and privacy guarantees

- The response **never** contains: provider class or provider name,
  secret-presence flags, `capability_report()` issue strings, table/schema
  names, stack traces, or private account data. Classification consumes only
  settings booleans, the fail-closed provider factory, the latest pipeline
  run status, and a bounded active-listing existence query — no network
  calls, no credential reads, no live provider probing.
- Staff diagnostics (`/staff/release-diagnostics/`, capability reports) stay
  staff-only; anonymous, ordinary-authenticated, and staff requests receive
  exactly the same public envelope shape.

## Caching and advisory semantics

- Results are cached in the Django cache with TTL
  `ASSISTANT_STATUS_CACHE_SECONDS` (default 30, clamped to ≤ 60; 0 disables
  caching), keyed only on the request's auth state.
- **Advisory only.** A stale or failed status response can never gate or
  ungate the actual send path. The POST endpoints in
  `crank/views/job_search.py` are unchanged and remain authoritative; their
  stable error envelopes (503 `assistant_unavailable`, 504 `provider_timeout`,
  429 `rate_limited`/`cost_limit`, 500 `invalid_output`/`service_error`) are
  the durable contract when the status check is wrong. A runtime outage
  between a `ready` status check and a send surfaces through those existing
  responses, not through this endpoint.
- `ready` is **not proof** of a working provider or populated inventory at
  send time; it only means the last bounded check found no blockers.
- `/healthz/ready/` and the deployment-time `crank/capability.py` fail-fast
  validation are unchanged and remain the deployment gate; do not substitute
  this endpoint for readiness probing.
