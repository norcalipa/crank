# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the read-only migration status command."""

import io
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.db.utils import DatabaseError

from crank.management.commands.migration_status import EXPECTED_TABLES


class MigrationStatusCommandTests(TestCase):
    command_path = "crank.management.commands.migration_status"

    def _executor(self, pending=(), applied=2, tables=EXPECTED_TABLES):
        loader = SimpleNamespace(
            applied_migrations={("crank", str(index)): None for index in range(applied)},
            graph=SimpleNamespace(
                leaf_nodes=lambda: [("crank", "0018_alter_agentrun_run_type")]
            ),
        )
        executor = SimpleNamespace(
            loader=loader,
            migration_plan=lambda leaves: [(migration, False) for migration in pending],
        )
        return executor, tables

    def _run(self, *args, **kwargs):
        stdout = io.StringIO()
        stderr = io.StringIO()
        result = call_command(
            "migration_status", *args, stdout=stdout, stderr=stderr, no_color=True, **kwargs
        )
        return result, stdout.getvalue(), stderr.getvalue()

    def test_clean_state_human_readable(self):
        executor, tables = self._executor()
        with patch(f"{self.command_path}.MigrationExecutor", return_value=executor), patch(
            f"{self.command_path}.connection.introspection.table_names",
            return_value=list(tables),
        ):
            result, output, _error = self._run()

        self.assertEqual(result, 0)
        self.assertIn("Migration status: clean", output)
        self.assertIn("Applied migrations: 2", output)
        self.assertIn("Pending migrations: 0", output)
        self.assertIn("crank.0018_alter_agentrun_run_type", output)
        self.assertIn("crank_jobsearchconversation: present", output)
        self.assertIn("crank_jobsearchmessage: present", output)

    def test_pending_state_json_is_machine_readable_and_fails(self):
        pending = [SimpleNamespace(app_label="crank", name="0010_jobsearchconversation_jobsearchmessage")]
        executor, tables = self._executor(pending=pending, applied=8, tables=(EXPECTED_TABLES[0],))
        with patch(f"{self.command_path}.MigrationExecutor", return_value=executor), patch(
            f"{self.command_path}.connection.introspection.table_names",
            return_value=list(tables),
        ):
            stdout = io.StringIO()
            with self.assertRaises(CommandError):
                call_command("migration_status", "--json", stdout=stdout, no_color=True)

        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "pending")
        self.assertEqual(report["applied_count"], 8)
        self.assertEqual(report["pending_count"], 1)
        self.assertEqual(report["pending"], [{"app": "crank", "name": pending[0].name}])
        self.assertFalse(report["tables"][EXPECTED_TABLES[1]])

    def test_pending_state_human_readable_lists_missing_table(self):
        pending = [SimpleNamespace(app_label="crank", name="0014_jobsourcecatalog_joblisting")]
        executor, tables = self._executor(pending=pending, tables=())
        with patch(f"{self.command_path}.MigrationExecutor", return_value=executor), patch(
            f"{self.command_path}.connection.introspection.table_names", return_value=[]
        ):
            stdout = io.StringIO()
            with self.assertRaises(CommandError):
                call_command("migration_status", stdout=stdout, no_color=True)

        output = stdout.getvalue()
        self.assertIn("Migration status: pending", output)
        self.assertIn("- crank.0014_jobsourcecatalog_joblisting", output)
        self.assertIn("crank_jobsearchconversation: MISSING", output)
        self.assertIn("crank_jobsearchmessage: MISSING", output)

    def test_database_error_json_is_valid_and_descriptive(self):
        with patch(
            f"{self.command_path}.MigrationExecutor",
            side_effect=DatabaseError("database is unavailable"),
        ):
            stdout = io.StringIO()
            with self.assertRaises(CommandError) as raised:
                call_command("migration_status", "--json", stdout=stdout, no_color=True)

        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "error")
        self.assertIsNone(report["applied_count"])
        self.assertIn("database is unavailable", report["error"])
        self.assertIn("database is unavailable", str(raised.exception))

    def test_database_error_human_path_raises_clear_command_error(self):
        with patch(
            f"{self.command_path}.MigrationExecutor",
            side_effect=DatabaseError("database is unavailable"),
        ):
            with self.assertRaisesRegex(CommandError, "Unable to read migration state"):
                self._run()
