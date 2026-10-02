# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Shape of migration 0046 (#474): a choices-only AlterField with no DDL."""

from django.db import connection
from django.db.migrations import AlterField
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase

NAME = "0046_alter_companyfieldevidence_state"
PARENT = "0045_jobsourcecatalog_consecutive_failures"


class CompanyFieldEvidenceStateMigrationTests(TestCase):
    def test_single_choices_only_operation_with_expected_parent(self):
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        migration = loader.disk_migrations[("crank", NAME)]
        self.assertEqual(migration.dependencies, [("crank", PARENT)])
        self.assertEqual(len(migration.operations), 1)
        operation = migration.operations[0]
        self.assertIsInstance(operation, AlterField)

        state = loader.project_state(("crank", PARENT))
        new_state = state.clone()
        operation.state_forwards("crank", new_state)
        old_field = state.models["crank", "companyfieldevidence"].fields["state"].clone()
        new_field = new_state.models["crank", "companyfieldevidence"].fields["state"].clone()
        for field in (old_field, new_field):
            field.set_attributes_from_name("state")
        editor = connection.schema_editor()
        self.assertFalse(editor._field_should_be_altered(old_field, new_field))
        self.assertEqual(
            [value for value, _ in new_field.choices][-2:], ["pending", "rejected"]
        )
