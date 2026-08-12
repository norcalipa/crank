<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Production migration recovery runbook (#348)

This runbook is for the production outage in which
`/api/agent/conversations/` returned HTTP 500 because
`crank_jobsearchconversation` was absent. It is intentionally an operator
procedure: this change ships the read-only evidence tool and runbook only and
makes **no production changes**.

> **Safety warning:** Do not use `--fake` unless the production schema has been
> independently inspected and proven equivalent to the migration operations.
> Never run more than one migration process concurrently.

## Preconditions and ownership

- Identify the currently deployed image SHA and use that exact image for every
  command below. Record the SHA, operator, UTC timestamps, and Kubernetes
  namespace in the incident issue.
- Use production database credentials through the normal secret mechanism; do
  not paste credentials into shell history, logs, tickets, or this repository.
- Pause or coordinate deploys while this procedure runs. The web Deployment
  has two replicas and an HPA, so do not run a migration in each web pod and
  do not rely on a replica count of one for serialization.
- Select one operator and one migration runner. The runner must have network
  access to the production database and the deployed image's `manage.py`.

## 1. Capture the pre-state (read-only)

Run from the deployed image with production settings and credentials. Save the
complete stdout/stderr output as incident evidence, including the command,
image SHA, timestamp, and exit status:

```sh
python manage.py migration_status --json > migration-status-pre.json
status=$?
printf 'migration_status exit=%s\n' "$status" | tee -a migration-status-pre.log
cat migration-status-pre.json
python manage.py showmigrations crank 2>&1 | tee showmigrations-pre.log
```

`migration_status --json` is read-only. A non-zero status is expected when
migrations are pending, but the JSON must still be retained. It reports the
applied count, pending migrations in plan order, current leaf node(s), and the
presence of `crank_jobsearchconversation` and `crank_jobsearchmessage`.

If the command reports a database error, stop and resolve connectivity or
credentials before attempting any schema change. Do not substitute a local or
staging database for production evidence.

## 2. Confirm a recoverable backup

Before changing the schema, confirm with the database owner or backup system
that a recoverable production backup exists. Record:

- backup/snapshot identifier;
- completion timestamp and database/instance identifier;
- retention/expiration time; and
- the most recent restore validation or restore-test evidence.

If the backup is missing, stale, unavailable, or cannot be restored, **abort**
and escalate. Do not continue merely because the migration is expected to be
additive.

## 3. Apply migrations exactly once

After the backup confirmation is attached to the incident, stop concurrent
application deploy/migration activity and run this command **once**, from the
single designated runner and the same image SHA used for the pre-state:

```sh
python manage.py migrate --noinput 2>&1 | tee migrate-recovery.log
migration_status=${PIPESTATUS[0]}
printf 'migrate exit=%s\n' "$migration_status" | tee -a migrate-recovery.log
exit "$migration_status"
```

The command must be allowed to finish and its complete output and exit status
must be captured. Do not run `migrate` from both Deployment replicas, from an
HPA-created pod, or concurrently from a shell/Job. Do not use `--fake` as a
shortcut. If it fails, stop; preserve the output and follow [rollback/abort](#7-rollbackabort-guidance).

## 4. Capture post-state and verify tables

Using the same image and credentials, capture both migration views again:

```sh
python manage.py migration_status --json > migration-status-post.json
status=$?
printf 'migration_status exit=%s\n' "$status" | tee -a migration-status-post.log
cat migration-status-post.json
python manage.py showmigrations crank 2>&1 | tee showmigrations-post.log
```

A successful recovery has exit status `0`, no pending migrations, and all
migration checkboxes through the current `crank` leaf marked `[X]`. Confirm the
JSON and an independent database inspection show these tables:

- `crank_jobsearchconversation`
- `crank_jobsearchmessage`
- `crank_jobsourcecatalog` (migration 0014)
- `crank_joblisting` (migration 0014)
- `crank_employeralias` (migration 0016)
- `crank_unresolvedemployer` (migration 0016)
- `crank_jobmatch` (migration 0017)

Migrations 0015 and 0018 change existing schema rather than adding a new
standalone table; their `[X]` state and successful migration output are still
required. If any expected table is absent or any migration remains pending,
do not proceed to normal verification—abort and escalate with the captured
outputs.

## 5. Authenticated Job Search Assistant smoke test

Use a test account or an approved authenticated production account, and record
request timestamps, endpoint, status code, correlation/request ID, and a
redacted response summary. Do not record message content, cookies, tokens, or
personal data. Through the normal UI or an authenticated API session:

1. `GET /api/agent/conversations/` to load/resume the active conversation;
2. `POST /api/agent/conversations/` to create a conversation (use
   `{"create_new": true}` when a fresh conversation is required);
3. `POST /api/agent/conversations/<id>/` with a small test message and a unique
   `idempotency_key`, confirming a successful response;
4. verify reset at
   `POST /api/agent/conversations/<id>/reset/` as supported; and
5. verify delete at
   `POST /api/agent/conversations/<id>/delete/` as supported, only for the
   designated test conversation.

At minimum, the conversation list request must return HTTP 200 and must no
longer produce the missing-table 500. Confirm the UI can load/resume, create,
and send a message. If reset/delete are not part of the chosen test path,
record why and verify them in a separate approved test account or staging
environment instead.

## 6. Monitor the verification window

For the agreed verification window (record its start and end), monitor the
application logs, error rate, and New Relic. Search for the original and
related errors, including:

- `ProgrammingError`;
- `Table 'crank.crank_jobsearchconversation' doesn't exist`;
- `Table 'crank.crank_jobsearchmessage' doesn't exist`; and
- any missing-table error for the 0014–0018 Phase 3 models.

Record the query/time window, result count, dashboard or log-search link, and
operator. A clean window should show no new schema `ProgrammingError` events.
Keep the pre-state JSON, migration output, post-state JSON, smoke-test
results, and monitoring evidence attached to issue #348.

## 7. Rollback/abort guidance

- **Before migration starts:** abort if credentials, connectivity, image SHA,
  serialization, or backup confirmation is uncertain.
- **Migration fails partway:** do not retry blindly and do not run a second
  concurrent migration. Preserve logs, keep application changes paused, ask
  the database/Django owner to inspect transaction and migration state, and
  restore from the confirmed backup only under the approved incident recovery
  procedure. Re-run `migration_status --json` only when the owner confirms it
  is safe and serialized.
- **Post-state or smoke test fails:** stop rollout, preserve all evidence, and
  escalate to the application/database owners. Do not mark migrations fake or
  manually edit `django_migrations` to hide the failure.
- **Rollback:** application image rollback does not automatically undo schema
  changes. Never reverse migrations or restore a database without an approved,
  backup-aware rollback plan and owner authorization; assess compatibility of
  the rolled-back image first.

Issue **#349** is the permanent prevention work: it will add deployment-time
migration execution/checking so a production rollout cannot silently omit
required migrations. This #348 recovery deliberately does not modify
manifests, workflows, or application code.
