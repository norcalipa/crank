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
