<!-- Copyright (c) 2024 Isaac Adams
Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Deployment Baseline — September 2026 (issue #455)

This document defines the reproducible readiness baseline established before
the contextual right-sidebar rollout (epic #454). It records the baseline
procedure, the reconciliation of the reviewed commit `d62183a` with deployed
contracts, and the staging replay used to reproduce the September 12 audit
findings without using a real user conversation.

Live deployment capture and operator activation (credentials, bootstrap)
remain owned by **#453**; this baseline supplies the instrument and the
evidence procedure, not operator execution.

## 1. The baseline instrument

`readiness_baseline` emits a single JSON record describing deployment and data
readiness. It is read-only and never prints secret values.

```bash
python manage.py readiness_baseline            # JSON on stdout
python manage.py readiness_baseline --out /tmp/baseline-$(git rev-parse --short HEAD).json
```

Record shape (staff-only evidence; not a public contract):

| Key | Meaning | Source |
| --- | --- | --- |
| `generated_at` | UTC timestamp of the capture | `django.utils.timezone.now()` |
| `env` | Declared deployment environment (`dev`/`staging`/`prod`) | `settings.ENV` |
| `source_version` | Backend git SHA / image identifier | `crank.release.git_sha()` (reads `GIT_SHA`, `SOURCE_VERSION`, …) |
| `frontend_build_id` | Webpack contenthash of the served bundle | `crank.release.frontend_build_id()` |
| `migrations` | `{applied_count, pending_count, status}`; `pending` ⇒ missing schema | `crank.release.migration_status_summary()` |
| `job_search_provider` | Selected job-search provider token (safe-token validated) | `crank.release._safe_job_search_provider()` |
| `capabilities` | Non-secret capability configuration (enabled flags + issues; secrets reduced to presence booleans) | `crank.capability.capability_report().to_dict()` |
| `inventory` | Bounded inventory health signals + violations | `crank.services.inventory_health.check_inventory_health()` |
| `latest_runs` | Latest `AgentRun` per run type: `{run_type, status, terminal, created, finished_at, error_summary}` | `crank.models.agent_run.AgentRun` |
| `source_counts` | `{configured, approved, enabled}` job sources | mirrors `crank.admin_dashboard._aggregate_counts()` |

Secret hygiene: configured values of `SECRET_KEY`, `LLM_API_KEY`,
`USAJOBS_AUTH_KEY`, `FIRECRAWL_API_KEY`, and `YELP_API_KEY` are scrubbed from
any free-text field (e.g. `error_summary`) before serialization, and tests in
`crank/tests/management/test_readiness_baseline.py` assert the values never
appear in the output.

## 2. The three distinguishable failure conditions

The record names each condition with its own field so they can never be
confused:

1. **Disabled provider/source** → `capabilities.capabilities[].enabled` flags
   and `capabilities.capabilities[].issues` (e.g. "LLM_API_KEY is missing"),
   plus `source_counts.approved` / `source_counts.enabled` = 0.
2. **Missing schema** → `migrations.status == "pending"` with
   `migrations.pending_count > 0` (or `status == "error"` when the loader
   cannot read migration state at all).
3. **Failed completed run** → `latest_runs[].status == "failed"` with
   `terminal: true` and a sanitized `error_summary`. Non-terminal latest runs
   (`pending`/`running`) are reported with `terminal: false`.

## 3. Reconciliation notes — reviewed commit `d62183a`

The September 12 audit reviewed `d62183acd4a7f93c662c1368f9aec6aeb1f839b8`
(`chore: update fats/DB host IPs after local subnet change`). Reconciliation
of the target checkout with deployed contracts:

- **Preference schema v2** — migration `0024_preference_schema_v2` migrates
  existing `UserPreference` documents to schema v2 (`compensation.
  require_public_company`, `work_location.max_in_office_days`) and bumps
  `schema_version` to 2. `0025_alter_userpreference_schema_version_and_more`
  follows with the field-level change. Any deployed database must show both
  applied in `migrations` (the leaf as of this baseline is
  `0029_merge_20260815_1645`, which also merges the twin `0028` files).
- **Source policy** — external source policy lives in the database via
  `SourceCatalog` (rating sources; see `docs/source-catalog.yaml` and
  `seeds/crank.organization.yaml`) and `JobSourceCatalog` (job sources;
  approval states `pending`/`approved`/`blocked` plus the `enabled` flag).
  Code never expands the URL allowlist (`APPROVED_JOB_SOURCE_DOMAINS`).
  The audit's "four configured sources, none approved/enabled" state is the
  expected `seed_job_sources` output: all rows `pending` + `enabled=False`
  until an operator approves them through the admin.
- **Actual run consumers** — every scheduled consumer is a
  `crank.models.agent_run.AgentRun` row keyed by `RunType`
  (`noop`, `gather_scores`, `job_pipeline`, `crawl_schedule`, `crawl`).
  `latest_runs` in the baseline is the authoritative per-type record; the
  audit's "no latest run" means `latest_runs` is empty (or, for
  `job_pipeline`, no terminal run has ever completed).

## 4. Staging fixture set

`seed_staging_baseline` (dev/staging only; refuses `ENV=prod`, idempotent)
loads the acceptance fixtures:

- **Companies and scores** — `Staging Example Corp` (target) and
  `Staging Rating Source` (rating giver) with one **active** `crank_score`
  row (`status=1`) and one **superseded** (`status=0`, deactivated) for the
  same type/source/target.
- **Company evidence** — three `CompanyProfileObservation` rows on
  `Staging Example Corp`: `accepted` (fresh), `stale` (`accepted` but
  `observed_at` 45 days old), `conflicted` (`conflict_fields=["rto_evidence"]`).
- **Jobs** — `Staging Baseline Source` (approved + enabled, usajobs adapter)
  with one `active` and one `expired` `JobListing`.
- **Match cases** — two `JobMatch` rows for the ordinary test account: one
  ordinary (organization-scores + compensation factors) and one
  **unknown-requirement** case whose factor names a requirement with no
  `JobCriteria` projection (`requirement:quantum_fluency`). Matching and UI
  code must treat unknown-requirement factors as neutral data.
- **Accounts** — `staging_user` (ordinary) and `staging_staff`
  (`is_staff=True`), both with the throwaway password
  `staging-baseline-throwaway` (override with `--password`). These are
  staging-only fixtures, never real credentials. The **signed-out** case is
  simply performing the replay without authenticating.

```bash
python manage.py seed_staging_baseline            # or: --password 'custom-throwaway'
python manage.py readiness_baseline               # baseline now reflects the fixtures
```

## 5. Staging replay procedure

### 5a. Reproduce the audit failures

From a **fresh** staging database (no fixtures loaded):

1. `python manage.py migrate` — confirm `readiness_baseline.migrations.status`
   is `clean`.
2. `python manage.py seed_job_sources` — creates the curated sources, all
   `pending` and disabled.
3. `python manage.py readiness_baseline` — expect the audit state:
   `source_counts = {configured: 4, approved: 0, enabled: 0}`,
   `inventory.violations` containing "no approved and enabled job sources",
   and `latest_runs` empty (no run consumer has ever executed).
4. Sign out (no session) and load the UI: the sidebar/assistant surfaces the
   audit's narrow mobile content and empty-data states. Record screenshots as
   the "before" evidence.

### 5b. Replay rankings → company details → chat → jobs as an ordinary test account

1. `python manage.py seed_staging_baseline` — load the acceptance fixtures.
2. Sign in as `staging_user` (throwaway password above; use an incognito
   profile — never a real user conversation).
3. Replay in order, recording outcomes at each step:
   - **rankings** — org rankings render from the active/superseded scores;
     confirm superseded scores do not appear as current.
   - **company details** — company profile shows the accepted observation;
     the stale and conflicted observations are distinguishable in review
     surfaces.
   - **chat** — send one scripted prompt; the reply uses the configured LLM
     capability (or surfaces the capability issue when unset).
   - **jobs** — job matches list the active listing; the expired listing is
     absent; the unknown-requirement match renders without error.
4. `python manage.py readiness_baseline --out baseline-after-replay.json` —
   archive the after-state as evidence for the rollout review.

## 6. Owners

Named at review time per the plan's open question; placeholders until then:

| Area | Owner |
| --- | --- |
| Frontend | _TBD at review_ |
| Backend | _TBD at review_ |
| Data | _TBD at review_ |
| Release | _TBD at review_ |

## 7. Boundaries

- No production activation, credential provisioning, or deployment execution
  (#453 owns those).
- No schema/migration changes, no new settings, no user-facing UI changes in
  this baseline.
- The record is staff-only evidence and is explicitly **not** a public API
  contract (public capability mapping is #457's scope).
