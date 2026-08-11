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
        # The crank migration graph must have a single leaf.
        app_leaf = loader.graph.leaf_nodes("crank")
        self.assertEqual(len(app_leaf), 1, "crank graph must have a single leaf")
        # The source models must be created by some migration in the chain.
        all_created = set()
        for (app, name), migration in loader.disk_migrations.items():
            if app != "crank":
                continue
            for op in migration.operations:
                if op.__class__.__name__ == "CreateModel":
                    all_created.add(op.name)
        self.assertIn("SourceCatalog", all_created)
        self.assertIn("SourceRun", all_created)
        self.assertIn("SourceCatalogAudit", all_created)

    def test_migration_depends_on_previous_leaf(self):
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        loader.build_graph()
        leaf = loader.graph.leaf_nodes("crank")[0]
        migration = loader.disk_migrations[leaf]
        # The leaf migration must depend on the previous crank migration in the chain.
        deps = [dep[1] for dep in migration.dependencies if dep[0] == "crank"]
        self.assertTrue(
            len(deps) == 1,
            "crank leaf migration must have exactly one crank dependency",
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
