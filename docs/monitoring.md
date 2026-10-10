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
Reason codes are a finite per-event set: the pipeline codes `none`, `timeout`,
`cost_limit`, `rejected`, `authorization`, `upstream`, and `internal`, plus the
assistant codes listed under [Assistant and data-freshness
metrics](#assistant-and-data-freshness-metrics). Unknown values are recorded as
`other`.

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

## Redis is a cache, not a system of record

Redis holds only derived, rebuildable data: page/API cache entries,
algorithm objects, and rate-limit counters (`job_search_rl:*`). Durable state
lives in MySQL — sessions use the DB session engine, and required
post-commit publication work is the `crank_publicationevent` outbox table
(issue #470, `docs/publication-outbox.md`), never a Redis stream or key.
Losing Redis is always safe: every value is recomputable from MySQL or
expires by TTL.

Public cached surfaces (`cache_page` on `algo/<int:algorithm_id>/`, the
funding/RTO choices endpoints, and the organization detail/scores/provenance
API caches) must never contain another account's private shell, history, or
preferences. Per-user surfaces — conversations, matches, preferences, and
their rate-limit/session keys — are never written to public cache keys, and
user match responses stay uncached. A regression test renders the cached
index and organization API responses for two different accounts and asserts
identical, user-data-free payloads.

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

## Assistant and data-freshness metrics

Issue #482 adds bounded, allowlisted `CrankOperation` events. Values are
enums, integers, booleans or UUIDs only; prompts, URLs, user ids and free text
are never recorded, and a client `X-Request-ID` that is not a UUID is dropped
(`correlation_id` is omitted).

- `assistant_turn`: `phase` attempted/saved/replied/failed/rejected/replayed;
  failures carry `reason_code` (provider_timeout, cost_limit, invalid_output,
  assistant_unavailable, conversation_gone, preference_stale,
  preference_version_unavailable, service_error, unexpected_error, worker_interrupted) and a
  `failure_stage`. `worker_interrupted` is emitted when a stale claim is reaped
  or taken over (a plain failed retry is not counted); rejections use rate_limited, turn_in_progress, retry_limited.
- `assistant_first_result`: once per conversation (conditional update on
  `JobSearchConversation.first_result_at`). `seconds_to_first_result` counts from
  the start of the current chat session (the earliest user message reachable
  without an idle gap over 30 minutes), not from conversation creation, so
  resumed and pre-deploy conversations do not report weeks.
  `turns_to_first_result` counts assistant turns across the whole conversation.
- `availability_state`: assistant status and job-matches surfaces.
- `preference_decision`: apply/dismiss/undo/reset with `status` applied/dismissed/undone/stale/invalid/failed
  and `origin` proposal (chat), direct (priorities editor) or reset. Apply and
  dismiss take the origin from the allowlisted label on the proposal token;
  undo takes it from the label stamped on the undo token. A reset records
  `decision=reset`, `origin=reset` itself. Tokens issued before deploy have no
  label and default to `proposal`.
- `matching_batch` gains `publication_lag_max_seconds` / `publication_lag_count`.
  Lag is measured only for users with a previous generation, matching the
  `publication_match_lag_seconds` gauge, so first generations are excluded. Failed
  `source_stage` events carry `failure_stage=source`; `matching_batch` carries
  `failure_stage=matching` when any user failed.
- The `repeated-failure` alert excludes `publication_sweep` and
  `preference_decision`; those failures are inspected via their own events.
- `pipeline_health` (emitted by `crawl_healthcheck`): queue, outbox, review and
  evidence-freshness gauges. `publication_sweep`: per-run sweep outcome.

Recovery: a failing `pipeline_health` emits `healthy=false` with a
`reason_code`; run `python manage.py crawl_status` and the admin readiness page.

### Baselines

The `metrics:` block in `monitoring.yaml` is `baseline_only`. No alert
threshold is added; thresholds are chosen after a 14-day baseline and feed
issue #492.

#### Release decision gates

Issue #492 adds a `release_gates:` block to `monitoring.yaml`. Gates are
evidence queries for staged release decisions (`docs/rollout-gates.md`,
"Contextual assistant staged release (#492)"). They are not alerts: nothing
pages, `metrics:` stays `baseline_only` and `alerts:` is unchanged.

- A **`provisional`** gate carries a number restated in
  `docs/rollout-gates.md`; its `source` names the heading that says where the
  number came from and whether it is new. It has not been confirmed against
  measured data.
- A **`baseline_required`** gate has `threshold: null`. Its query can be run
  and its value recorded, but it cannot pass or fail until a baseline exists.

**What "sample" means.** Every gate has a `sample_nrql` and a `min_sample`
floor. The sample counts only events that carry the measurement the gate
reads, so an error event, or an event emitted by another phase, cannot meet a
floor:

| Gate | Sample (`sample_nrql`) |
|---|---|
| `kind: nrql`, ratio query (`a / b`) | the denominator of the ratio — a share of nothing is not a measurement |
| `kind: nrql`, any other aggregate (`percentile`, `max`) | the number of events in the window that carry the attribute the query aggregates |
| `kind: nrql`, a gauge that reads 0 when nothing happened, or a stock that exists before the phase starts (`publication-outbox-age`, `evidence-stale-share`) | the phase's own activity, not the events the value reads. `publication-outbox-age` reads `max(outbox_oldest_age_seconds)` over the probe's `pipeline_health` events (a degraded event, `healthy: false`, carries no gauge) but its sample is `sum(processed)` over completed `publication_sweep` events: an empty outbox with the consumer off reads age 0 on every probe tick, and the probe sends at most 96 events a day whatever the consumer does. `evidence-stale-share` samples `sum(items_succeeded)` over completed `company_profile_crawl` stages (the crawler reports `completed` for an empty or wholly rejected fetch, which persists nothing and adds 0), because the accepted rows already exist and the freshness policy (90 to 365 days per field) cannot move a share inside a 24-hour window. A window with no sweep or crawl holds |
| `kind: alerts_quiet` | the number of `signal_event` events in the window that match `signal_filter`: the event the named alerts read, narrowed to the ones this phase emits with a measurement. "No alert opened" is also true when nothing emits, so quiet counts only while the signal is live |

**A sample counts requests and events, not people.** Telemetry has no user
or session dimension, and the availability endpoints send one event per request
with no throttle, so one signed-in account polling in a loop (300 polls make 300
events) or one open tab can meet any floor, and moderated sessions and smoke
tests count in the same windows. `assistant-ready-share` therefore counts only
`cached = false` events, at most one per cache period for all signed-in users.
`job-matches-source-unavailable` and the two success shares have no such
filter: choose their floors knowing N is a request count, and say so when
locking them.

The three signal filters, and why each exists:

| Gate | `signal_filter` | Without it the floor could be met by |
|---|---|---|
| `interactive-alerts-quiet` | `status = 'provider_succeeded'` | `interactive_call` events with `status: error`, which the chat view emits for every rejected or failed request |
| `job-source-alerts-quiet` | `enabled_sources IS NOT NULL` | degraded `inventory_health` events (`healthy: false`, no gauges) emitted when the probe itself cannot run |
| `matching-alerts-quiet` | `stage = 'match_recompute'` | `matching_batch` events with `stage: job_pipeline_matching`, which the job pipeline of the earlier `job_source` phase emits every six hours |

`publication-to-match-lag` carries the same `stage = 'match_recompute'`
filter for the same reason: the job pipeline's `matching_batch` event also
has `publication_lag_*` attributes.

**What "value" means.**

- `kind: nrql`: the single number the `nrql` query returns when it is run at
  the end of the window.
- `kind: alerts_quiet`: the number of the gate's named alerts that were
  **open at any time in the window**, read from the alerting tool's incident
  history (not by re-running the alert queries, whose own windows are minutes
  or hours). An alert that was already open when the window started counts.
  This is why step 4 below needs the owner to have confirmed the alerts exist
  in the tool: without a policy there is no history to read. "Exist" means the
  alert's query, threshold and window equal its entry under `alerts:` in
  `docs/monitoring.yaml`, and it has been seen to open once. Seen to open is
  the check that matters for `deadline-resource-pressure`, which sums
  `deadline_reached`, an attribute sent as a boolean; whether the alerting tool
  sums it to a non-zero number is not tested here (see the pending decision in
  `docs/usability-validation.md`). No quiet gate names `rejection-spike`: it
  sums `items_failed`, which the job pipeline's `source_stage` events do not
  carry.

**A clean window.** A gate's queries end `SINCE <window> ago`, so a result
describes the phase only if the whole window lies after the phase was
enabled. The window is clean when both hold:

- at least `window` has passed since the phase's post-merge check succeeded
  (`docs/rollout-gates.md`, "Durable enablement rule", rule 5); and
- during the window the phase's switch was not turned off and no job source
  was disabled. A rollback exercise (`docs/usability-validation.md`, "What
  only the owner can do") therefore restarts the window of every gate in the
  phase it touches.

Without this, `job-matches-source-unavailable` would breach for seven days
after `job_source` is enabled, because every signed-in `job_matches` poll
before then emitted `no_source` or `source_disabled`.

**Evaluation procedure.** Apply the steps in order to one gate. The first
step whose check is true decides the outcome; later steps are not read.

| Step | Check | Outcome |
|---|---|---|
| 1 | `min_sample` is `null` | hold |
| 2 | the window is not clean | hold |
| 3 | the sample is missing or below `min_sample` | hold |
| 4 | `kind: alerts_quiet` and `policy_confirmed` is not `true` | hold |
| 5 | `threshold` is `null`, or the query returned no value | hold |
| 6 | the value is `operator` the `threshold` (`above`: value > threshold; `below`: value < threshold) | breach |
| 7 | none of the above | pass |

A value equal to the threshold is a pass under either operator.

**Phase decision.** A phase's gates are combined like this:

1. Any gate is **breach** — do not expand. Follow that gate's `on_breach`
   (hold or roll back).
2. Otherwise, any gate is **hold** — the decision is **hold**. A held gate
   blocks expansion; it is never read as a pass.
3. Every gate is **pass** — the phase may be expanded, if the non-telemetry
   entries in its decision row are also met. Expanding is still the owner's
   decision.

**Worked examples.** The floors and thresholds below are invented for the
examples only. They are not defaults and are not proposed values.

*Ratio gate* — `interactive-reply-success` (`operator: below`,
`threshold: 0.90`), supposing the owner had locked `min_sample: 200` and the
window is clean:

| `sample_nrql` (attempted turns) | `nrql` (replied ÷ attempted) | Decided at | Outcome |
|---|---|---|---|
| 150 | 0.97 | step 3: 150 is below 200 | hold |
| 240 | 0.88 | step 6: 0.88 is below 0.90 | breach |
| 240 | 0.90 | step 7 | pass |
| 240 | 0.93 | step 7 | pass |

As checked in (`min_sample: null`) all four are hold at step 1.

*Aggregate gate* — `publication-outbox-age` (`operator: above`), supposing
`min_sample: 20` and a clean window:

| `sample_nrql` (items processed by completed sweeps) | `nrql` (oldest age in the window, seconds) | `threshold` | Decided at | Outcome |
|---|---|---|---|---|
| 24 | 5400 | `null` (as checked in) | step 5 | hold |
| 24 | 5400 | 3600 | step 6: 5400 is above 3600 | breach |
| 24 | 600 | 3600 | step 7 | pass |
| 0 (consumer off, or an empty outbox) | 0 | 3600 | step 3: 0 is below 20 | hold |
| 0 | no value | 3600 | step 3 | hold |

*Alerts-quiet gate* — `matching-alerts-quiet` (`operator: above`,
`threshold: 0`), supposing `min_sample: 500` and a clean window:

| `sample_nrql` (`match_recompute` batches) | Named alerts open in the window | `policy_confirmed` | Decided at | Outcome |
|---|---|---|---|---|
| 2016 | 0 | `false` (as checked in) | step 4 | hold |
| 2016 | 0 | `true` | step 7 | pass |
| 2016 | 1 (`matching-backlog` opened on day 3) | `true` | step 6: 1 is above 0 | breach |
| 2016 | 1 (already open when the window started) | `true` | step 6 | breach |
| 0 (CronJob still suspended) | 0 | `true` | step 3 | hold |

*Phase* — `match_recompute` with `publication-to-match-lag` on hold and
`matching-alerts-quiet` passing: the phase decision is **hold**.

No gate can pass as checked in: every `min_sample` is `null` and every
`policy_confirmed` is `false`. A floor of 1 would let a single event decide a
release (one replied turn is 100%), so no floor is invented here; the floors
are listed with the other
[decisions pending owner confirmation](usability-validation.md#decisions-pending-owner-confirmation).
`window` is the `SINCE` clause of the gate's queries. The two gates that read
`pipeline_health` gauges (`max()` for `publication-outbox-age`, `latest()` for
`evidence-stale-share`) use a 24-hour window; the 14 days in their phase's
decision row is how long the phase is observed before deciding. For
`publication-outbox-age` that 24-hour window is the only limit on what `max()`
sees: a stall on day 3 is not in the value read at the end, so either read the
gate daily during the 14 days or move it to `SINCE 14 days ago` when the floor
is locked.

**Locking a gate.** After at least 14 days of data with the capability
enabled, a small follow-up pull request sets `min_sample` (and `threshold`
where it is `null`) from the observed baseline, sets `policy_confirmed: true`
once the owner has checked the alert policy, and changes `status` to `locked`.
Adding an alert or changing a `baseline_only` flag belongs in that same pull
request, with the tests that pin them. Gates reading `inventory_health` or
`pipeline_health` have no data while the `crank-healthcheck` CronJob is
suspended.

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
