# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.

# Production migration deployments

Production deploys use one controlled migration runner before the web
Deployment is changed. The deploy workflow renders `k8s/migrate-job.yml` with
the image SHA being deployed, applies the ConfigMap, deletes any stale Job with
the same image name, creates the Job, and waits for it to finish. The web
Deployment is applied only after the Job reports `Complete`. A failed Job or a
900-second timeout fails the workflow and leaves the existing web rollout in
place; the workflow prints Job logs and a description for diagnosis.

`update-home-deployment.yml` follows the same sequence with the fixed image tag
`latest`, so its Job name is `crank-migrate-latest`. The workflow-level
`deployment` concurrency group prevents overlapping workflow runs. The Job has
`backoffLimit: 0`, a deadline, and a unique name per image tag. This is
intentional: exactly one migration runner must own schema changes. Running
migrations in every web pod is unsafe because the Deployment starts two
replicas and the HPA can add more replicas, allowing concurrent DDL and
conflicting migration attempts.

## Rollout rules

1. Build and publish the immutable image SHA.
2. Verify the image exists in GHCR.
3. Apply the ConfigMap, then run and verify the migration Job against that
   exact image and the production database.
4. Apply the web Deployment and wait for the normal Kubernetes rollout.
5. Confirm `/healthz/ready/` returns HTTP 200 and reports
   `{"status":"ready","pending_migrations":0}`.

The migration Job and application use the same `crank-config` ConfigMap,
`db-connect-credentials` Secret, `SECRET_KEY`, image pull secret, database host
alias, and non-root security settings. Cluster operators must ensure those
resources exist in namespace `crank`, that the database credentials can run
Django migrations, and that the deploying identity has permission to create,
read, delete, wait on, and fetch logs for Jobs and Pods.

Schema changes must be backward-compatible during a rolling deployment. Use
expand-and-contract migrations: add nullable or optional structures first,
deploy code that can work with both old and new schema, backfill separately,
then remove old structures only after all old pods are gone. Do not combine a
column/table removal or incompatible constraint with code that can still be
served by an old replica. Migrations are forward-only in production; do not
rewrite an applied migration or roll the database schema backward as part of
an application rollback.

## Cross-branch migration numbering and ordering

Concurrent PRs regularly ship migrations at the same time. Two rules keep
the graph deployable:

1. **A number claim is not an ordering.** Migration numbers avoid file
   collisions, but the dependency graph — not the filename — decides the
   deploy order. Every merged branch must leave exactly **one head**.
2. **Never create another branch's migration.** Only the owning ticket
   writes its own migration file, against the latest landed `main` head.

A migration that depends on the same parent as a migration on another open
branch creates **sibling leaves**; merging both heads would leave the graph
with no single deployable head, so whichever PR merges second must repair
the graph **before merging**:

- rebase its migration onto the newly landed head (a one-line dependency
  change; the number stays with its owning ticket), or
- add the next numbered merge migration following the `0029` precedent.

Never renumber or rewrite an applied migration.

### Current allocations and ordering assumptions

- `0029_merge_20260815_1645` is the landed `main` head (verified
  2026-09-15: `makemigrations --check --dry-run` clean, single head).
- **0030 → #458** (turn delivery state), carried by PR #496
  (`0030_jobsearch_turn_state`, parent `0029`).
- **0031 → #470** (PublicationEvent outbox), reserved from parent `0029`
  per the epic numbering contract recorded by PR #499.
- **0032 → #461** (ScoreTupleAnchor), carried by this branch
  (`0032_score_tuple_anchor`, parent `0029`).

PR #495 and PR #496 both sit on parent `0029`, so exactly one ordering
assumption is recorded here: **#495 (#461) is expected to merge first**
(it is in fix-verification while #496 is still in round-1 review). When
#495 lands, `0032` becomes the head; #496 must then rebase
`0030_jobsearch_turn_state` onto the landed `0032` (one-line dependency
change) or add the next numbered merge migration before merging. If the
order reverses and #496 lands first, this branch's rule applies in the
mirror direction: `0032_score_tuple_anchor` must be rebased onto the
landed `0030` (or paired with a numbered merge migration) before #495
merges. Whichever PR merges second owns that repair; the other side is
not modified by the first PR's branch.

## Readiness and liveness

`/healthz/ready/` uses Django's read-only `MigrationExecutor` plan to check for
unapplied migrations. It returns 200 only when the plan is empty, and 503 for
pending migrations or an unavailable database. It is unauthenticated so the
kubelet can call it. Its timeout is three seconds because the check touches the
production database.

The liveness probe intentionally remains `/`. A pending migration makes a pod
unready and removes it from Service endpoints, but must not make Kubernetes
restart it indefinitely. Liveness answers whether the process is alive;
readiness answers whether it is safe to receive traffic.

## Backup and rollback

Before a production migration, verify that the latest database backup exists,
is complete, and has been tested or can be restored. Record the backup/time and
the image SHA in the deployment record. For destructive or high-risk changes,
take an on-demand backup and rehearse the restore/rollback plan first.

If the migration Job fails, do not apply the Deployment. Inspect the Job logs,
fix the migration or database prerequisite, and retry with a new image or the
same image after the cause is corrected. If an already-applied application
release must be rolled back, deploy code that remains compatible with the
current schema. Restore the database only when necessary, after stopping
writes and following the verified backup recovery procedure; database restore
is disruptive and may lose writes after the backup point.

## Debugging a failed Job

From the cluster host, replace `SHA` with the image tag used by the failed
deploy:

```sh
JOB=crank-migrate-SHA
k3s kubectl -n crank get job "$JOB" -o wide
k3s kubectl -n crank describe job "$JOB"
k3s kubectl -n crank logs "job/$JOB" --all-containers=true
k3s kubectl -n crank get pods -l job-name="$JOB" -o wide
```

Check, in order: that `crank-config`, `db-connect-credentials`, and
`crank-secrets` exist; that `fats` is reachable on port 3306; that the database
user has the required DDL privileges; that the image contains the expected
migration files; and that no incompatible migration is already partially
applied. The Job is retained for 24 hours by `ttlSecondsAfterFinished`, which
leaves time to collect logs. After remediation, rerun the deploy gate rather
than manually applying the web Deployment first.

## CI protection

The Python workflow runs `python manage.py makemigrations --check --dry-run`
and migrates a fresh SQLite database, then verifies that the migration plan is
empty. The trigger no longer ignores `crank/migrations/**`; migration changes
must run these checks instead of being silently skipped.

## Epic #454 rollout: baseline, numbering contract, and additive plan

Recorded by [#463](https://github.com/norcalipa/crank/issues/463) (UX-37,
phase 0) before any epic schema file is generated. This section is the
contract every later epic schema PR follows; it records state and rules, it
does not create migrations.

### Verified baseline

Verified at commit `d62183acd4a7f93c662c1368f9aec6aeb1f839b8` (2026-09-14):

- The migration graph has a **single head**:
  `crank/migrations/0029_merge_20260815_1645.py`. It merges the two `0021_*`
  branches (`0021_companyrequest`, `0021_crawl_freshness`) and the two
  `0028_*` branches
  (`0028_jobsearchconversation_helpfulness_gap_emitted`,
  `0028_remove_agentrun_unique_agentrun_running_per_type_and_more`).
- The highest existing migration number is **0029**.
  `0024_preference_schema_v2` is the deployed preference document schema
  (v2).
- Verified with `python manage.py makemigrations --check --dry-run`
  ("No changes detected") and `python manage.py migration_status`. The
  operator must still confirm the **actually deployed** revision against
  production before the first epic migration runs (#463: "confirm the actual
  deployed revision before implementation").

### Migration numbering contract

1. New epic schema files start at **0030**. **One owning ticket per file**;
   a file is created only by its owning ticket, at implementation time,
   against the latest `main`.
2. Assignments recorded so far. Owners are verified against each
   issue's own scope (checked 2026-09-14), not guessed:
   - **0030 → #458** (UX-05, turn delivery state) —
     `0030_jobsearch_turn_state` already exists on the `fix/issue-458`
     branch; the number must not be reused.
   - **0031 → #470** (UX-28) — `PublicationEvent` outbox table, parent
     `0029_merge_20260815_1645` (per controller resolution); in flight.
   - **0032 → #461** (score tuple anchor) —
     `0032_score_tuple_anchor.py` already exists on the `fix/issue-461`
     branch (PR #495); the number must not be reused.
   - **0033** and **0034** are merge migrations
     (`0033_merge_20260915_0828`, `0034_merge_0031_publicationevent_0033_merge_20260915_0828`)
     that already exist on `main`; they join prior branch leaves and are not
     available to a schema ticket.
   - **0035 → #459** (UX-14, versioned preference schema), parent `0034`;
     assigned 2026-09-19.
   - **0036 → #460** (UX-18, accepted field-level evidence) —
     `0036_company_field_evidence`, parent `0035_preference_schema_v3`;
     assigned 2026-09-20.
   - **0037 → #466** (UX-15, preference propose/apply/revision/undo) —
     `0037_userpreference_revision`, parent `0036_company_field_evidence`;
     assigned 2026-09-21 (reassigned from the earlier "0037+ controller
     merge order" placeholder: #466 is the preference-lifecycle ticket and
     `UserPreference.revision` is a preference-lifecycle field that #467
     merely reads; one owning ticket per file).
   - **0038 → #467** (UX-17, result revisions) —
     `0038_jobmatch_result_revision`, parent `0037_userpreference_revision`;
     assigned 2026-09-22.
   - **0039 → #475** (UX-29, versioned recompute fields) —
     `0039_match_result_generation`, parent `0038_jobmatch_result_revision`;
     assigned 2026-09-22.
3. Cross-branch numbering collisions are resolved with a **numbered merge
   migration** following the `0029` precedent. Never renumber or rewrite an
   applied migration.
4. No partial unique constraints: production MySQL does not support them
   (Django's W036 warnings are expected). New uniqueness uses an
   unconditioned `UniqueConstraint` or `select_for_update` serialization
   (precedent: `crank/services/scores.py` `_persist_locked`).
5. Every epic schema PR ends with `python manage.py makemigrations --check
   --dry-run` clean (CI-enforced).

### Field-group rollout plan (expand → compat deploy → backfill → contract)

Each group below follows the expand-and-contract rules in the Rollout rules
section above: expand additively, deploy code that reads both shapes, run a
bounded reviewed backfill, and contract only after all old pods are gone.

| Field group | Owning ticket | Migration slot | Rollout sequence |
|---|---|---|---|
| Preference fields | #459 (UX-14, "Version the preference schema") | 0035 (assigned 2026-09-19) | Add nullable/defaulted JSON keys and columns first; deploy code that serves both document shapes; bounded resumable backfill of existing v2 documents; no removal in the same release. |
| Turn lifecycle | #458 (UX-05, "Persist turn delivery state") | 0030 (claimed on `fix/issue-458`) | Additive turn/status columns; code tolerates missing values on old rows; backfill lifecycle timestamps separately; old conversations stay replayable via `idempotency_key`. |
| Accepted field-level evidence | #460 (UX-18, "Model accepted field-level evidence, scope and freshness timestamps") | 0036 (`0036_company_field_evidence`, parent `0035`) | Additive evidence model/rows; accepted evidence is resolved per field, never by blanket latest-row; pending/rejected/conflicting observations stay inspectable. The provenance API payload gains `fields`/`unverified_fields` without a cache-key change — the key constructor also drives outbox invalidation, so versioning it would desynchronize the sweep; instead `crank.views.api.organization_provenance` treats a cached entry with no `fields` key as a miss and rebuilds it, so pre-deploy entries never serve an evidence-less modal for the rest of the TTL. Both `ACCEPTED`-producing surfaces — the crawler's `AUTO_APPLIED` path and the admin accept action — write evidence in the same transaction as the review state. |
| Preference revision | #466 (UX-15, "Implement preference propose, apply, revision checks and undo services") | 0037 (`0037_userpreference_revision`, parent `0036`) | Additive defaulted `revision` column (existing rows start at 0); no backfill, no index; old pods never select or write the column and their `expected_modified` path keeps working; the service accepts both `expected_modified` and `expected_revision` during the mixed-pod window, and chat callers flip to `expected_revision` after the column is everywhere. |
| Publication outbox | #470 (UX-28, publication after commit) | 0031 | Create `PublicationEvent` additively; the consumer is gated by its own switch (see the capability registry in `docs/rollout-gates.md`); pending work survives restart; an incompatible consumer rollback is addressed before enablement. |
| Result revisions | #467 (UX-17, "Unify deterministic eligibility, match reasons and result revisions") | 0038 (`0038_jobmatch_result_revision`, parent `0037`) | Additive nullable revision columns (`preference_revision`, `data_revision`, `generated_at`, `requirements`, `evidence_ids`) on `JobMatch`; the unique key `(user, listing, preference_version, ranker_version)` is unchanged; readers fall back to the un-revised result; bounded backfill of revisions for existing matches. |
| Versioned recomputation | #475 (UX-29, "Recompute versioned matches") | 0039 (`0039_match_result_generation`, parent `0038`) | Additive `MatchResultState` model (per-user generation ticket/tags) plus `JobMatch.result_generation`; no backfill — existing rows keep `result_generation=NULL` and are served by the live fallback until the drain publishes a generation; obsolete-run rejection via compare-and-swap; preserve seen/dismissed across upserts. See `docs/match-recompute.md`. |

Tickets that do **not** own schema in this epic (verified scopes): #457
(UX-04) is assistant/inventory availability exposure, #462 (UX-24) is the
ingestion owner, and #479 (UX-11) is navigation-state sharing — none of
them appears in the table above.

Existing v1/v2 preference meanings, conversations, idempotency keys, and
seen/dismissed state must survive deployment and backfill.
`crank/tests/test_schema_compat.py` is the compatibility harness those PRs
keep green; its bounded-backfill test demonstrates the resumable pattern
(a second run is a no-op).

Mixed-version preference writes are covered on both sides of a rolling
deploy: `read`/`export` serve additive shapes unchanged, and the old
`apply_patch`/`reset` paths validate and rewrite only the fields their
schema version knows, preserving unknown additive fields verbatim — never
validating, modifying, dropping, or projecting them into markdown. Patches
that target unknown fields are rejected (`UnknownFieldError`), so an old
pod can never corrupt a newer pod's fields.

Idempotent turn replay is guaranteed on both backends: the conditional
`unique_jobsearch_message_idempotency` constraint enforces first-write-wins
where partial indexes exist (SQLite), and on production MySQL — which does
not create partial unique indexes (W036 expected) — writers are serialized
on the parent conversation row (`persist_idempotent_message` in
`crank/views/job_search.py`, following `crank/services/scores.py`
`_persist_locked`). The MySQL two-connection race is proven by the
operator-run variant `crank/tests/test_mysql_concurrency.py` (skipped by
default on SQLite; exact run commands are in its docstring).

### Independent rollback controls

Every new capability ships with its own `CapabilitySwitch` key or settings
flag, default **off**, added to `ALLOWED_CAPABILITY_KEYS` by its owning
ticket before its code path is enabled anywhere (registry:
`docs/rollout-gates.md`, "Capability Registry"). Rollback is a switch/flag
flip rehearsed by `rollback_drill`; it never reverses production migrations
or deletes records to recover an unavailable provider, and it must leave
direct controls, stored conversations, preferences, and accepted data
usable.

## Sibling migration leaves and merge order

`0030_jobsearch_turn_state` (PR #496), `0031_publicationevent` (PR #504,
issue #470), and `0032_score_tuple_anchor` (PR #495) are sibling leaves:
all declare `0029_merge_20260815_1645` as their only parent, so the
migration graph forks into three parallel heads. The agreed ordering
contract: **whoever merges second or third adds a dependency/merge
migration** on top of the landed leaves so the graph returns to a single
truthful head — `makemigrations --check` (and the CI gate above) fails
loudly on the forked graph until that merge migration exists. Do not
renumber or rewrite an already-applied leaf to fake a linear history; add
the merge node instead, and record the resulting ordering in the deployment
record for the release that carries both changes. This PR (#504 / #470) was
created as the third sibling off `0029_merge_20260815_1645`; when #458 or
#461 lands first, whichever branch lands later updates `0031`'s dependency
from `0029_merge_20260815_1645` to the then-latest landed head (likely
`0030_jobsearch_turn_state` or `0032_score_tuple_anchor`) and adds a
numbered merge migration if a head split remains, keeping
`makemigrations --check --dry-run` clean on the merged result.

## Allocation: 0040 (issue #468)

- **0040 → #468** (`0040_source_refresh_state`, parent
  `0039_match_result_generation`): additive `last_attempt_at` and
  `consecutive_failures` on `SourceCatalog` and `JobSourceCatalog`, plus a
  bounded backfill of NULL `last_crawl_at` from the latest SUCCESS `CrawlRun`
  (never PARTIAL). The backfill is idempotent and its reverse is a no-op. No
  constraints or partial indexes. Old pods ignore the new columns, so rollback
  is a code-only redeploy. **0041 → #477** builds on top of it.
