# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Migration-plan tests for the Phase 2 source catalog (issue #311).

These verify that the schema the models declare is fully captured by a single
coherent crank migration (so ``makemigrations --check`` stays clean in CI) and
that the new model tables/constraints are produced by migration operations.
"""

from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TransactionTestCase

from crank.models.source import ApprovalState  # noqa: F401  (imported for state)


class SourceMigrationTests(TransactionTestCase):
    def test_source_models_have_a_migration(self):
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        loader.build_graph()
        # All crank models are covered by migrations: no pending changes.
        from django.core.management.commands.makemigrations import Command

        # Heavy check avoided; instead verify the declared models are in a migration.
        app_leaf = loader.graph.leaf_nodes("crank")
        self.assertEqual(len(app_leaf), 1, "crank graph must have a single leaf")
        leaf = app_leaf[0]
        self.assertEqual(leaf[0], "crank")
        # The newest crank migration must create the source models.
        migration = loader.disk_migrations[leaf]
        created = [
            op.name
            for op in migration.operations
            if op.__class__.__name__ == "CreateModel"
        ]
        self.assertIn("SourceCatalog", created)
        self.assertIn("SourceRun", created)
        self.assertIn("SourceCatalogAudit", created)

    def test_migration_depends_on_previous_leaf(self):
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        loader.build_graph()
        leaf = loader.graph.leaf_nodes("crank")[0]
        migration = loader.disk_migrations[leaf]
        # The migration continues the chain from 0011 (single-leaf history).
        self.assertTrue(
            any(dep[1].startswith("0011") for dep in migration.dependencies),
            "source migration must depend on the previous crank migration",
        )

    def test_no_pending_model_changes(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        try:
            # If models and migrations disagree, makemigrations emits an error.
            call_command(
                "makemigrations",
                "--check",
                "--dry-run",
                "crank",
                verbosity=0,
            )
        except CommandError as exc:  # pragma: no cover - failure signal
            self.fail(f"Detected unmigrated model changes: {exc}")
