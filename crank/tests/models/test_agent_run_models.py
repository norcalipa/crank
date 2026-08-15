# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase

from crank.models.agent_run import AgentRun


class AgentRunModelTests(TestCase):
    def test_agent_run_creation_defaults(self):
        run = AgentRun.objects.create(run_type=AgentRun.RunType.NOOP)
        self.assertIsNotNone(run.correlation_id)
        self.assertEqual(run.status, AgentRun.Status.PENDING)
        self.assertEqual(run.counts, {})
        self.assertEqual(run.error_summary, "")
        self.assertIsNone(run.started_at)
        self.assertIsNone(run.finished_at)
        self.assertEqual(str(run), "noop [pending]")

    def test_agent_run_finalize_sets_counts_and_timestamps(self):
        run = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.RUNNING
        )
        run.finalize(
            AgentRun.Status.SUCCEEDED,
            counts={"items_seen": 3, "items_failed": 0},
        )
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.SUCCEEDED)
        self.assertEqual(run.counts, {"items_seen": 3, "items_failed": 0})
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.error_summary, "")

    def test_agent_run_finalize_failure_summary(self):
        run = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.RUNNING
        )
        run.finalize(AgentRun.Status.FAILED, error_summary="boom")
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.FAILED)
        self.assertEqual(run.error_summary, "boom")

    def test_only_one_running_run_per_type(self):
        AgentRun.objects.create(run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.RUNNING)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AgentRun.objects.create(
                    run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.RUNNING
                )

    def test_terminal_statuses_do_not_conflict_with_running_lock(self):
        # A terminal (succeeded) run must not block a new running run.
        AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.SUCCEEDED
        )
        second = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.RUNNING
        )
        self.assertEqual(second.status, AgentRun.Status.RUNNING)

    def test_finalize_rejects_invalid_transition(self):
        run = AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.SUCCEEDED
        )
        with self.assertRaises(ValueError):
            run.finalize(AgentRun.Status.RUNNING)

    def test_running_blocks_pending(self):
        """A RUNNING row must block a PENDING row for the same run_type."""
        AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP,
            status=AgentRun.Status.RUNNING,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AgentRun.objects.create(
                    run_type=AgentRun.RunType.NOOP,
                    status=AgentRun.Status.PENDING,
                )

    def test_pending_blocks_running(self):
        """A PENDING row must block a RUNNING row for the same run_type."""
        AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP,
            status=AgentRun.Status.PENDING,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AgentRun.objects.create(
                    run_type=AgentRun.RunType.NOOP,
                    status=AgentRun.Status.RUNNING,
                )

    def test_pending_blocks_pending(self):
        """Two PENDING rows for the same run_type must be rejected."""
        AgentRun.objects.create(
            run_type=AgentRun.RunType.NOOP,
            status=AgentRun.Status.PENDING,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AgentRun.objects.create(
                    run_type=AgentRun.RunType.NOOP,
                    status=AgentRun.Status.PENDING,
                )


class AgentRunMigrationDataCleanupTests(TransactionTestCase):
    """Verify the 0028 migration's RunPython de-duplicates active rows."""

    def test_deduplicate_active_runs_keeps_oldest(self):
        """The migration data cleanup should keep the oldest active row and
        mark extras as SKIPPED so the new constraint can be applied.

        The new partial unique constraint is already applied in the test DB,
        so we temporarily drop it to insert duplicate rows (simulating the
        pre-migration state), then run the deduplication function and verify.
        """
        import importlib
        from django.db import connection

        migration_mod = importlib.import_module(
            "crank.migrations.0028_remove_agentrun_unique_agentrun_running_per_type_and_more"
        )
        deduplicate_active_runs = migration_mod.deduplicate_active_runs

        # Temporarily drop the constraint so we can insert duplicate rows.
        with connection.cursor() as cursor:
            cursor.execute(
                "DROP INDEX IF EXISTS unique_agentrun_active_per_type"
            )
            try:
                first = AgentRun.objects.create(
                    run_type=AgentRun.RunType.JOB_PIPELINE,
                    status=AgentRun.Status.PENDING,
                )
                second = AgentRun.objects.create(
                    run_type=AgentRun.RunType.JOB_PIPELINE,
                    status=AgentRun.Status.RUNNING,
                )
                third = AgentRun.objects.create(
                    run_type=AgentRun.RunType.JOB_PIPELINE,
                    status=AgentRun.Status.RUNNING,
                )
                # A different run_type should be untouched.
                other = AgentRun.objects.create(
                    run_type=AgentRun.RunType.NOOP,
                    status=AgentRun.Status.PENDING,
                )

                from django.apps import apps as django_apps

                class FakeSchemaEditor:
                    pass

                deduplicate_active_runs(django_apps, FakeSchemaEditor())

                first.refresh_from_db()
                second.refresh_from_db()
                third.refresh_from_db()
                other.refresh_from_db()

                self.assertEqual(first.status, AgentRun.Status.PENDING)
                self.assertEqual(second.status, AgentRun.Status.SKIPPED)
                self.assertEqual(third.status, AgentRun.Status.SKIPPED)
                self.assertEqual(other.status, AgentRun.Status.PENDING)
            finally:
                # Recreate the constraint so the DB is in a clean state.
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "unique_agentrun_active_per_type ON crank_agentrun (run_type) "
                    "WHERE status IN ('running', 'pending')"
                )