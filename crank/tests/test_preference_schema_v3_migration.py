# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Migration compatibility tests for 0035_preference_schema_v3 (issue #459).

Follows the precedent in
``crank.tests.models.test_agent_run_models.AgentRunMigrationDataCleanupTests``:
the migration module is imported by dotted path (its filename is not a valid
Python identifier) and its RunPython functions are called directly against
the live app registry with a dummy schema editor, since neither function
performs schema DDL.
"""
import copy
import importlib

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase

from crank.models.preference import UserPreference, default_preferences
from crank.services import preferences as preferences_service

User = get_user_model()

_migration = importlib.import_module("crank.migrations.0035_preference_schema_v3")
migrate_to_v3 = _migration.migrate_to_v3
reverse_migration = _migration.reverse_migration


class _FakeSchemaEditor:
    """RunPython functions here never touch the schema editor."""


def _v2_document():
    """A schema-v2 document: the v3 default minus the v3-only keys."""
    doc = default_preferences()
    del doc["roles"]
    del doc["importance"]
    del doc["scope"]
    for key in (
        "basis", "period", "minimum_total_compensation",
        "equity_liquidity_required", "acceptable_liquidity_events",
    ):
        del doc["compensation"][key]
    del doc["work_location"]["office_days_exact"]
    return doc


class PreferenceSchemaV3MigrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("v3mig", password="secret")

    def _run_forward(self):
        migrate_to_v3(django_apps, _FakeSchemaEditor())

    def _run_reverse(self):
        reverse_migration(django_apps, _FakeSchemaEditor())

    def test_v1_default_document_gains_v3_keys(self):
        """A v1-shaped document (also missing the 0024 v2 keys) gains the
        full v3 key set and lands at schema_version 3."""
        doc = _v2_document()
        del doc["compensation"]["require_public_company"]
        del doc["work_location"]["max_in_office_days"]
        pref = UserPreference.objects.create(user=self.user, preferences=doc, schema_version=1)

        self._run_forward()
        pref.refresh_from_db()

        self.assertEqual(pref.schema_version, 3)
        self.assertEqual(pref.preferences["roles"], {"families": [], "titles": [], "seniority": []})
        self.assertEqual(pref.preferences["importance"], {})
        self.assertEqual(pref.preferences["scope"], {"countries": [], "role_families": []})
        self.assertEqual(pref.preferences["compensation"]["basis"], "base")
        self.assertEqual(pref.preferences["compensation"]["period"], "year")
        self.assertIsNone(pref.preferences["compensation"]["minimum_total_compensation"])
        self.assertIsNone(pref.preferences["compensation"]["equity_liquidity_required"])
        self.assertEqual(pref.preferences["compensation"]["acceptable_liquidity_events"], [])
        self.assertIsNone(pref.preferences["work_location"]["office_days_exact"])
        # 0024 keys are untouched by this migration (still absent).
        self.assertNotIn("require_public_company", pref.preferences["compensation"])
        self.assertNotIn("max_in_office_days", pref.preferences["work_location"])

    def test_v2_default_document_gains_v3_keys(self):
        doc = _v2_document()
        pref = UserPreference.objects.create(user=self.user, preferences=doc, schema_version=2)

        self._run_forward()
        pref.refresh_from_db()

        self.assertEqual(pref.schema_version, 3)
        self.assertEqual(pref.preferences, default_preferences())

    def test_v2_document_with_exclusions_and_custom_values_preserved(self):
        doc = _v2_document()
        doc["compensation"]["minimum_salary"] = 175000
        doc["compensation"]["currency"] = "EUR"
        doc["exclusions"]["companies"] = ["BadCo"]
        doc["exclusions"]["titles"] = ["Recruiter"]
        doc["notes"] = "prefers remote"
        doc["priorities"] = {"culture": 0.8}
        pref = UserPreference.objects.create(user=self.user, preferences=doc, schema_version=2)
        before = copy.deepcopy(pref.preferences)

        self._run_forward()
        pref.refresh_from_db()

        self.assertEqual(pref.schema_version, 3)
        for key, value in before.items():
            if key in ("compensation", "work_location"):
                for sub_key, sub_value in value.items():
                    self.assertEqual(pref.preferences[key][sub_key], sub_value)
            else:
                self.assertEqual(pref.preferences[key], value)
        self.assertEqual(pref.preferences["roles"], {"families": [], "titles": [], "seniority": []})

    def test_document_with_additive_unknown_keys_preserved(self):
        """A document already carrying keys from a *future* (post-v3)
        schema version keeps them verbatim through the v3 backfill."""
        doc = _v2_document()
        doc["notifications"] = {"channel": "email"}
        doc["compensation"]["future_field"] = "staff"
        pref = UserPreference.objects.create(user=self.user, preferences=doc, schema_version=2)

        self._run_forward()
        pref.refresh_from_db()

        self.assertEqual(pref.schema_version, 3)
        self.assertEqual(pref.preferences["notifications"], {"channel": "email"})
        self.assertEqual(pref.preferences["compensation"]["future_field"], "staff")
        self.assertEqual(pref.preferences["roles"], {"families": [], "titles": [], "seniority": []})

    def test_second_forward_run_is_a_noop(self):
        doc = _v2_document()
        pref = UserPreference.objects.create(user=self.user, preferences=doc, schema_version=2)

        self._run_forward()
        pref.refresh_from_db()
        modified_after_first = pref.modified
        preferences_after_first = copy.deepcopy(pref.preferences)

        self._run_forward()
        pref.refresh_from_db()

        self.assertEqual(pref.modified, modified_after_first)
        self.assertEqual(pref.preferences, preferences_after_first)

    def test_reverse_migration_restores_exact_document_and_schema_version(self):
        doc = _v2_document()
        doc["compensation"]["minimum_salary"] = 140000
        doc["culture"] = ["transparent"]
        pref = UserPreference.objects.create(user=self.user, preferences=doc, schema_version=2)
        original = copy.deepcopy(pref.preferences)

        self._run_forward()
        self._run_reverse()
        pref.refresh_from_db()

        self.assertEqual(pref.schema_version, 2)
        self.assertEqual(pref.preferences, original)

    def test_reverse_migration_is_a_noop_on_already_v2_shaped_document(self):
        doc = _v2_document()
        pref = UserPreference.objects.create(user=self.user, preferences=doc, schema_version=2)
        modified_before = pref.modified

        self._run_reverse()
        pref.refresh_from_db()

        self.assertEqual(pref.modified, modified_before)
        self.assertEqual(pref.schema_version, 2)

    def test_roundtrip_export_equal_before_and_after_forward_and_reverse(self):
        doc = _v2_document()
        doc["compensation"]["minimum_salary"] = 190000
        UserPreference.objects.create(user=self.user, preferences=doc, schema_version=2)

        exported_before = preferences_service.export(self.user)

        self._run_forward()
        self._run_reverse()

        exported_after = preferences_service.export(self.user)

        self.assertEqual(exported_before["preferences"], exported_after["preferences"])
        self.assertEqual(exported_before["schema_version"], exported_after["schema_version"])
