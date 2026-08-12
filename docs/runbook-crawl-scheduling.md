<!-- Copyright (c) 2024 Isaac Adams
Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

Owner: maintainer (crank.fyi)
Last reviewed: 2026-08-12
Version/change process: update this runbook with every scheduling or rollout-policy change.

# Crawl scheduling runbook

The company-profile and job-listing freshness planners are disabled by default.
They only dispatch rows that are approved, enabled, and older than their
configured freshness TTL. The planner records aggregate `scheduled`, `stale`,
`skipped`, and `errors` counters in the `crawl_planning` telemetry event; it
never includes organization names, URLs, provider payloads, or credentials.

## Staging enablement

1. Review the approved/enabled `SourceCatalog` and `JobSourceCatalog` rows and
   provision provider credentials through the existing Kubernetes Secret. Do
   not put credentials in a ConfigMap or repository file.
2. Set `AGENT_RUN_ENABLED=true` and `CRAWL_CRON_ENABLED=true` in
   `crank-agent-config`. Start with the defaults (`168` hours for organization
   profiles and `24` hours for job listings), then adjust
   `ORGANIZATION_FRESHNESS_HOURS` or `JOB_FRESHNESS_HOURS` if the source terms
   and provider budget support a tighter target.
3. Apply `k8s/crank-crawl-cron.yaml` with both CronJobs still suspended. Run a
   one-off bounded smoke test first:

   ```sh
   kubectl -n crank create job --from=cronjob/crank-crawl-jobs crawl-smoke-$(date +%s)
   kubectl -n crank logs -l job-name=<smoke-job-name> --all-containers
   ```

4. Inspect the command's aggregate counters and source timestamps. Unsuspend
   only the phase that has passed the smoke test:

   ```sh
   kubectl -n crank patch cronjob crank-crawl-organizations -p '{"spec":{"suspend":false}}'
   kubectl -n crank patch cronjob crank-crawl-jobs -p '{"spec":{"suspend":false}}'
   ```

## Production override and rollback

The checked-in schedules are `0 */6 * * *` for organization profiles and
`*/15 * * * *` for jobs. Override `ORGANIZATION_CRAWL_CRON` and
`JOB_CRAWL_CRON` in deployment configuration, then update the CronJob `spec.schedule`
explicitly; Kubernetes does not interpolate Django environment variables into a
CronJob schedule. Keep `concurrencyPolicy: Forbid`, the database-backed
`crawl_schedule` singleton guard, source limits, and deadline guardrails.

To pause without deleting resources, set either CronJob's `spec.suspend=true`
and/or set `CRAWL_CRON_ENABLED=false` (the command exits without claiming work).
If a provider is failing, pause that phase, leave its source timestamp stale for
a bounded retry after remediation, and inspect `crawl_planning` telemetry before
resuming. A manual bounded dispatch is available with:

```sh
python manage.py schedule_crawls --phase jobs --max-sources 1 --deadline-seconds 60
```

The command still requires both `AGENT_RUN_ENABLED` and `CRAWL_CRON_ENABLED`;
these switches are intentional kill switches.
