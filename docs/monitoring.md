<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Operations monitoring and controls

Crank emits two best-effort New Relic custom event types:

- `AgentRun` is the compatibility event for scheduled lifecycle transitions.
- `CrankOperation` is the bounded Phase 4 event. Its `event_name` is one of
  `interactive_call`, `scheduled_run`, `source_stage`, `matching_batch`, or
  `operational_change`.

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
and `reason_code`.

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

## Admin controls and recovery

Staff-only Django admin views expose sanitized `AgentRun`/`SourceRun` history,
source approval/enabled state, last success/failure and bounded counters. The
`CapabilitySwitch` model provides kill switches for existing capabilities (for
example `interactive_agent` and `job_pipeline`); it does not create arbitrary
execution controls. Job-source and capability bulk actions require the explicit
`confirm=yes` confirmation marker. Every confirmed change records actor,
timestamp (`created`), target, action, old value, new value, and confirmation in
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
