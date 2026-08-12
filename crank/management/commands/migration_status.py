# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Report Django migration and required-table state without changing the schema."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import DatabaseError


EXPECTED_TABLES = (
    "crank_jobsearchconversation",
    "crank_jobsearchmessage",
)


class Command(BaseCommand):
    help = "Report migration state and required Job Search Assistant tables (read-only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit a machine-readable JSON report.",
        )

    def _error_report(self, exc):
        return {
            "applied_count": None,
            "current_leaf_nodes": [],
            "error": f"Unable to read migration state from the database: {exc}",
            "pending": [],
            "pending_count": None,
            "status": "error",
            "tables": {},
        }

    def _report(self):
        executor = MigrationExecutor(connection)
        loader = executor.loader
        leaf_nodes = loader.graph.leaf_nodes()
        plan = executor.migration_plan(leaf_nodes)
        pending = [
            {"app": migration.app_label, "name": migration.name}
            for migration, backwards in plan
            if not backwards
        ]
        table_names = set(connection.introspection.table_names())
        tables = {table: table in table_names for table in EXPECTED_TABLES}
        return {
            "applied_count": len(loader.applied_migrations),
            "current_leaf_nodes": [
                {"app": app, "name": name} for app, name in leaf_nodes
            ],
            "pending": pending,
            "pending_count": len(pending),
            "status": "pending" if pending else "clean",
            "tables": tables,
        }

    def _write_human_report(self, report):
        self.stdout.write("Migration status: {}".format(report["status"]))
        self.stdout.write("Applied migrations: {}".format(report["applied_count"]))
        self.stdout.write("Pending migrations: {}".format(report["pending_count"]))
        for migration in report["pending"]:
            self.stdout.write(
                "  - {app}.{name}".format(
                    app=migration["app"], name=migration["name"]
                )
            )
        self.stdout.write("Current leaf nodes:")
        for leaf in report["current_leaf_nodes"]:
            self.stdout.write("  - {app}.{name}".format(**leaf))
        self.stdout.write("Expected tables:")
        for table, present in report["tables"].items():
            self.stdout.write("  - {}: {}".format(table, "present" if present else "MISSING"))

    def handle(self, *args, **options):
        as_json = options["as_json"]
        try:
            report = self._report()
        except DatabaseError as exc:
            report = self._error_report(exc)
            if as_json:
                self.stdout.write(json.dumps(report, sort_keys=True))
            raise CommandError(report["error"])

        if as_json:
            self.stdout.write(json.dumps(report, sort_keys=True))
        else:
            self._write_human_report(report)

        if report["pending"]:
            raise CommandError(
                "Pending migrations remain; this read-only command made no schema changes."
            )
        return 0
