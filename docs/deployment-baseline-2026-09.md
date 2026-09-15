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
| `migration_leaves` | Exact deployed revision: bounded per-app applied leaf migrations plus the exact pending leaves (`{applied, applied_count, pending, pending_count, truncated, status}`) | `readiness_baseline.migration_leaves()` (bounded by 25 identifiers per list) |
| `fixtures` | `{present, revision}` — whether the staging fixture set is loaded (detected via its unique marker rows) and its revision (`staging-baseline-1`) | `readiness_baseline.fixture_set()` |
| `readiness_gates` | Individually named, non-secret gate booleans: `adapter_registered`, `adapter_count`, `adapters`, `usajobs_adapter_registered`, `firecrawl_adapter_registered`, `credentials_configured`, `usajobs_credentials_configured`, `firecrawl_credentials_configured`, `pipeline_enabled`, `scheduler_enabled` | `readiness_baseline.readiness_gates()` (mirrors `crank.admin_dashboard._readiness_gates()`; reads the code-owned job adapter registry, never credential values) |
| `job_search_provider` | Selected job-search provider token (safe-token validated) | `crank.release._safe_job_search_provider()` |
| `capabilities` | Non-secret capability configuration (enabled flags + issues; secrets reduced to presence booleans) | `crank.capability.capability_report().to_dict()` |
| `inventory` | Bounded inventory health signals + violations | `crank.services.inventory_health.check_inventory_health()` |
| `latest_runs` | Latest `AgentRun` per run type: `{run_type, status, terminal, created, finished_at, error_summary}` | `crank.models.agent_run.AgentRun` |
| `source_counts` | `{configured, approved, enabled}` job sources | mirrors `crank.admin_dashboard._aggregate_counts()` |

Secret hygiene: every non-empty configured value of `SECRET_KEY`,
`LLM_API_KEY`, `USAJOBS_AUTH_KEY`, `FIRECRAWL_API_KEY`, and `YELP_API_KEY`
is scrubbed from any free-text field before serialization — unconditionally,
with no minimum length (short staging/test values are scrubbed just like
production ones, longest-first so overlapping values scrub correctly). Tests
in `crank/tests/management/test_readiness_baseline.py` assert configured
values — including short and overlapping ones — never appear in the output.

## 2. The three distinguishable failure conditions

The record names each condition with its own field so they can never be
confused:

1. **Disabled provider/source** → `capabilities.capabilities[].enabled` flags
   and `capabilities.capabilities[].issues` (e.g. "LLM_API_KEY is missing"),
   plus `source_counts.approved` / `source_counts.enabled` = 0. Unmet
   adapter/credential gates are recorded separately and unconditionally in
   `readiness_gates` (e.g. `usajobs_credentials_configured: false`,
   `firecrawl_credentials_configured: false`), because a disabled capability
   is considered OK by `capability_report()` and would otherwise hide the
   missing credential.
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

`seed_staging_baseline` (dev/staging only; refuses `ENV=prod`, idempotent,
fixture-set revision `staging-baseline-1` — reported by `readiness_baseline`
under `fixtures.revision`) loads the acceptance fixtures:

- **Rankings dependencies** — the default `ScoreAlgorithm`
  (`DEFAULT_ALGORITHM_ID`) and its `ScoreAlgorithmWeight` for the fixture
  score type, because `IndexView` resolves `DEFAULT_ALGORITHM_ID` and its
  ranking SQL inner-joins `crank_scorealgorithmweight`. Without these rows a
  fresh database renders empty rankings regardless of the scores.
- **Companies and scores** — `Staging Example Corp` (target) and
  `Staging Rating Source` (rating giver) with one **active** `crank_score`
  row (`status=1`) and one **superseded** (`status=0`, deactivated) for the
  same type/source/target.
- **Company evidence** — three `CompanyProfileObservation` rows on
  `Staging Example Corp`: `accepted` (fresh, strictly the latest), `stale`
  (`accepted` but `observed_at` 45 days old), `conflicted`
  (`conflict_fields=["rto_evidence"]`, one hour older than the accepted row so
  the ordinary provenance surface returns the accepted row).
- **Jobs** — `Staging Baseline Source` (approved + enabled, usajobs adapter)
  with one `active` and one `expired` `JobListing`.
- **Match cases** — two `JobMatch` rows for the ordinary test account: one
  ordinary (organization-scores + compensation factors) and one
  **unknown-requirement** case whose factor names a requirement with no
  `JobCriteria` projection (`requirement:quantum_fluency`). Matching and UI
  code must treat unknown-requirement factors as neutral data.
- **Accounts** — `staging_user` (ordinary, non-privileged) with the throwaway
  password `staging-baseline-throwaway` (override with `--password`), and
  `staging_staff` (`is_staff=True`). The staff account is created with an
  **unusable** password: it only ever receives a password through the
  explicit `--staff-password` provisioning flag (a secure operator-supplied
  value, never a repo-known default), and re-running the command without the
  flag never resets an already-provisioned password. The **signed-out** case
  is simply performing the replay without authenticating.

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

1. `python manage.py seed_staging_baseline` — load the acceptance fixtures
   (the command also seeds the default `ScoreAlgorithm` and its
   `ScoreAlgorithmWeight`; provision the staff account's password separately
   with `--staff-password <secure-value>` only if a staff sign-in is needed).
2. Sign in as `staging_user` (throwaway password above; use an incognito
   profile — never a real user conversation).
3. Replay in order, recording outcomes at each step:
   - **rankings** — org rankings render from the seeded scores: the command
     now seeds the `ScoreAlgorithm`/`ScoreAlgorithmWeight` rows the rankings
     consumer (`IndexView`) requires, so the target organization actually
     appears on a fresh database. Note the current consumer averages every
     score of a type for a target (its SQL does not filter `crank_score.status`),
     so the fixture's active and superseded rows both feed the average; the
     active/superseded distinction itself is fixture data, verified by the
     seed and reflected in the baseline record, not by this replay step.
   - **company details** — the ordinary provenance surface
     (`GET /api/organizations/<id>/provenance/`) returns the **accepted**
     observation as the latest one (the conflicted row is seeded strictly
     older). Distinguishing stale vs. conflicting evidence from one another
     requires the multi-row staff-only admin review surface; an
     ordinary-account surface for that comparison does not exist today —
     see the open question in §6.
   - **chat** — send one scripted prompt; the reply uses the configured LLM
     capability (or surfaces the capability issue when unset).
   - **jobs** — job matches list the active listing; the expired listing is
     absent; the unknown-requirement match renders without error.
4. `python manage.py readiness_baseline --out baseline-after-replay.json` —
   archive the after-state as evidence for the rollout review.

## 6. Owners

AC-4 requires named frontend, backend, data, and release owners in this
document. **This criterion is not yet met and is explicitly recorded as an
open question for the maintainer** rather than silently deferred:

- Option A — the maintainer supplies the four owner names in the PR review;
   they are added here in a one-line doc update before merge.
- Option B — the criterion is renegotiated (e.g. the named owners move to the
   #453 activation runbook or the rollout review), and this section records
   the renegotiated decision.

Until one of those happens, the table stays explicitly unconfirmed:

| Area | Owner |
| --- | --- |
| Frontend | *unconfirmed — open question (see PR review thread)* |
| Backend | *unconfirmed — open question (see PR review thread)* |
| Data | *unconfirmed — open question (see PR review thread)* |
| Release | *unconfirmed — open question (see PR review thread)* |

### Open questions recorded from review

1. **AC-4 owners** — supply or renegotiate the four named owners (above).
2. **Ordinary-visible evidence surface** — distinguishing stale vs.
   conflicting company-profile evidence requires the multi-row staff-only
   admin review surface; no ordinary-account surface exposes it today. If
   the ordinary replay must verify that distinction end-to-end, that is a
   product decision to add an ordinary-visible evidence consumer (UI work
   outside this issue's scope per the plan's non-goals); until then, the
   replay in §5b records only what existing surfaces can show.

## 7. Boundaries

- No production activation, credential provisioning, or deployment execution
  (#453 owns those).
- No schema/migration changes, no new settings, no user-facing UI changes in
  this baseline.
- The record is staff-only evidence and is explicitly **not** a public API
  contract (public capability mapping is #457's scope).
