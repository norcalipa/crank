# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Migration: add explicit role/seniority, compensation basis/period/total,
liquidity, exact office-day, importance and scope keys to existing preference
documents (schema v3, issue #459).
"""
from django.db import migrations, models

_COMPENSATION_DEFAULTS = {
    "basis": "base",
    "period": "year",
    "minimum_total_compensation": None,
    "equity_liquidity_required": None,
    "acceptable_liquidity_events": [],
}
_WORK_LOCATION_DEFAULTS = {
    "office_days_exact": None,
}
_TOP_LEVEL_DEFAULTS = {
    "roles": {"families": [], "titles": [], "seniority": []},
    "importance": {},
    "scope": {"countries": [], "role_families": []},
}


def migrate_to_v3(apps, schema_editor):
    UserPreference = apps.get_model("crank", "UserPreference")
    for pref in UserPreference.objects.all().iterator():
        doc = pref.preferences
        changed = False

        comp = doc.get("compensation", {})
        for key, default in _COMPENSATION_DEFAULTS.items():
            if key not in comp:
                comp[key] = default
                changed = True
        doc["compensation"] = comp

        wl = doc.get("work_location", {})
        for key, default in _WORK_LOCATION_DEFAULTS.items():
            if key not in wl:
                wl[key] = default
                changed = True
        doc["work_location"] = wl

        for key, default in _TOP_LEVEL_DEFAULTS.items():
            if key not in doc:
                doc[key] = default
                changed = True

        if changed:
            pref.preferences = doc
            pref.schema_version = 3
            pref.save(update_fields=["preferences", "schema_version", "modified"])


def reverse_migration(apps, schema_editor):
    UserPreference = apps.get_model("crank", "UserPreference")
    for pref in UserPreference.objects.all().iterator():
        doc = pref.preferences
        changed = False

        comp = doc.get("compensation", {})
        for key in _COMPENSATION_DEFAULTS:
            if key in comp:
                del comp[key]
                changed = True
        doc["compensation"] = comp

        wl = doc.get("work_location", {})
        for key in _WORK_LOCATION_DEFAULTS:
            if key in wl:
                del wl[key]
                changed = True
        doc["work_location"] = wl

        for key in _TOP_LEVEL_DEFAULTS:
            if key in doc:
                del doc[key]
                changed = True

        if changed:
            pref.preferences = doc
            pref.schema_version = 2
            pref.save(update_fields=["preferences", "schema_version", "modified"])


class Migration(migrations.Migration):
    dependencies = [
        ("crank", "0034_merge_0031_publicationevent_0033_merge_20260915_0828"),
    ]

    operations = [
        migrations.RunPython(migrate_to_v3, reverse_migration),
        migrations.AlterField(
            model_name="userpreference",
            name="schema_version",
            field=models.PositiveIntegerField(
                default=3,
                help_text="Version of the preferences JSON document schema.",
                verbose_name="schema version",
            ),
        ),
        migrations.AlterField(
            model_name="userpreferenceaudit",
            name="schema_version",
            field=models.PositiveIntegerField(default=3),
        ),
    ]
