<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Operations monitoring and controls

Crank emits two best-effort New Relic custom event types:

- `AgentRun` is the compatibility event for scheduled lifecycle transitions.
- `CrankOperation` is the bounded Phase 4 event. Its `event_name` is one of
  `interactive_call`, `scheduled_run`, `source_stage`, `matching_batch`,
  `operational_change`, `inventory_health`, `job_search_turn`,
  `job_search_tool_invocation`, or `job_search_helpfulness_gap`.

Only stable operational attributes are accepted: run type, status, stage,
registered adapter key, reason code, bounded counters, latency/duration,
freshness, token counters, and estimated cost. Prompts, model responses, source
bodies, credentials, arbitrary URLs, and user IDs are never event attributes.
Reason codes are the finite set `none`, `timeout`, `cost_limit`, `rejected`,
`authorization`, `upstream`, and `internal`.

## Dashboard and alert queries

The checked-in query definitions in [`monitoring.yaml`](./monitoring.yaml) are
the source of truth for a New Relic dashboard and alert policy. They intentionally
use `FACET` only on the bounded dimensions `run_type`, `stage`, `source_key`,
`reason_code`, and `tool` (per-datasource tool invocation).

The dashboard answers:

- last successful scheduled run and run duration;
- interactive-call and scheduled throughput;
- failures by finite reason code and per-source stage;
- source freshness (time since `last_seen_at`/last successful event);
- estimated interactive usage and cost; and
- deadline/resource pressure and matching backlog counters.

Alerts are recovery-oriented: repeated failures, no recent success, deadline
or resource pressure, estimated cost limit, rejection spikes, and backlog.
Each alert links to the runbook action: inspect sanitized admin runs, disable
the affected source/capability, fix the upstream or capacity issue, then
re-enable only after a confirmed healthy run.

## Job inventory health probe

The read-only `python manage.py crawl_healthcheck` command computes bounded
inventory signals (sources total, enabled sources, active listings, stale
sources, repeated crawl failures, collapsed sources, and unregistered
adapters) and emits a single `inventory_health` `CrankOperation` event. It
exits non-zero when unhealthy, so a Kubernetes CronJob
(`k8s/crank-healthcheck-cron.yaml`, suspended by default) can surface the
failure, and the same event drives the inventory alert policy:

- `zero-enabled-sources`: `enabled_sources = 0` (bootstrap not run or all
  sources disabled).
- `zero-active-listings`: `active_listings = 0` with at least one enabled
  source (crawl has never produced data).
- `stale-inventory`: `stale_sources > 0` (enabled sources past their
  `JOB_FRESHNESS_HOURS` freshness target).
- `repeated-failures`: `repeated_failure_sources > 0` (a source's last
  `CRAWL_REPEATED_FAILURE_THRESHOLD` crawls all failed or timed out).
- `listing-collapse`: `collapsed_sources > 0` (a source that previously
  ingested listings now reports zero active listings).

The probe is safe to run before the crawl is enabled and never requires
provider credentials.

If the probe itself cannot run (for example the database is unreachable), it
emits a degraded `inventory_health` event with `healthy = false` and a
`reason_code`, then exits non-zero. Keep Kubernetes-level alerting on CronJob
failures enabled so infrastructure outages surface even when the event
pipeline is down.

## Assistant helpfulness (job-search chat)

Issue #397 added telemetry to the job-search assistant so an operator can spot
"chat is useless" regressions (for example a demo/echo provider leaking into
production) before users hit them.

Per assistant turn the orchestrator emits a `job_search_turn` event with only
scalar counters: `tools_called`, `result_count`, `cited_ids_count`,
`empty_result` (the turn produced no result card), `inventory_nonempty`, and
`latency_ms`/`latency_bucket`. Each bounded datasource tool also emits a
`job_search_tool_invocation` event with `tool` and its `result_count`. When a
conversation has accumulated several assistant turns but produced no result
card, the view emits a `job_search_helpfulness_gap` event once per
conversation (an atomic flag guarantees exactly-once emission even under
concurrency) with `turns_without_result` and `empty_result` (see
`MIN_HELPFUL_TURNS`). No prompt, response, or conversation-identifier content
is ever an event attribute.

**Reading the telemetry to detect a useless chat:**

- Alert on a sustained high rate of `job_search_turn` where `empty_result` is
  true **and** `inventory_nonempty` is true — the assistant is engaged but not
  surfacing any server-grounded citations. (An empty catalog legitimately
  yields `empty_result`; empty inventory is reported separately via
  `inventory_nonempty`.)
- Alert on any `job_search_helpfulness_gap` event — it means real users are
  holding multi-turn conversations that never produce a single result card.
- Monitor the `EchoReplyError` / anti-echo guard: the orchestrator rejects a
  reply that restates the user turn without tool work when inventory is
  non-empty. A spike in helper rejected-turn counts (`reason_code = rejected`
  on `interactive_call`) signals either a bad provider or a too-aggressive
  prompt.
- Trend `latency_bucket` per provider; a jump in `gt1000` alongside rising
  `empty_result` suggests the gateway is timing out before grounding data.

**Deployment check.** The `crank.W001` system check warns loudly when
`JOB_SEARCH_PROVIDER=demo` in a non-dev environment (`ENV` of `prod`/`staging`).
Run `python manage.py check --deploy` in CI: a demo provider
in a production config must never silently serve simulated replies.

## Admin controls and recovery

Staff-only Django admin views expose sanitized `AgentRun`/`SourceRun` history,
source approval/enabled state, last success/failure and bounded counters. The
`CapabilitySwitch` model provides kill switches for existing capabilities (for
example `interactive_agent` and `job_pipeline`); it does not create arbitrary
execution controls.

**How confirmation works.** Every gated action on the company-request, rating
source, job-source, and capability-switch admins shares a single intermediate
confirmation step (`ConfirmableAdminActionMixin`). Selecting items and clicking
the action in the changelist re-renders a **confirmation page** that lists the
selected objects and the action. The operator reviews it and clicks
**Confirm and apply**; the form then re-POSTs the same action with a hidden
`confirm=yes` (plus csrf / `_selected_action` / `action` / `index`). Nothing is
mutated on the first click — the action body no-ops until the operator
confirms explicitly. Operators never need to hand-add `confirm=yes`; the UI
supplies it. Every confirmed change records actor, timestamp (`created`),
target, action, old value, new value, and `confirmed=True` in
`OperationalChangeAudit`. Non-staff users cannot view or mutate these models.

No admin action executes a run. A scheduled command remains the only execution
path, and all existing approval, enablement, overlap, deadline, and provider
limits continue to apply.

## Test and deployment procedure

Run `python manage.py migrate` before deploying the admin controls. Verify the
dashboard with mocked `newrelic.agent` events in tests, then perform one enabled
fixture-backed run. For an incident: capture the sanitized run ID/correlation
ID, disable the source or capability with confirmation, verify the next run is
skipped/isolated, remediate, and re-enable with a second audited confirmation.
