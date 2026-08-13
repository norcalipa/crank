# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Migration: add require_public_company and max_in_office_days to existing preference documents (schema v2)."""
from django.db import migrations


def migrate_to_v2(apps, schema_editor):
    UserPreference = apps.get_model("crank", "UserPreference")
    for pref in UserPreference.objects.all():
        doc = pref.preferences
        changed = False
        comp = doc.get("compensation", {})
        if "require_public_company" not in comp:
            comp["require_public_company"] = None
            doc["compensation"] = comp
            changed = True
        wl = doc.get("work_location", {})
        if "max_in_office_days" not in wl:
            wl["max_in_office_days"] = None
            doc["work_location"] = wl
            changed = True
        if changed:
            pref.preferences = doc
            pref.schema_version = 2
            pref.save(update_fields=["preferences", "schema_version", "modified"])


def reverse_migration(apps, schema_editor):
    UserPreference = apps.get_model("crank", "UserPreference")
    for pref in UserPreference.objects.all():
        doc = pref.preferences
        changed = False
        comp = doc.get("compensation", {})
        if "require_public_company" in comp:
            del comp["require_public_company"]
            doc["compensation"] = comp
            changed = True
        wl = doc.get("work_location", {})
        if "max_in_office_days" in wl:
            del wl["max_in_office_days"]
            doc["work_location"] = wl
            changed = True
        if changed:
            pref.preferences = doc
            pref.schema_version = 1
            pref.save(update_fields=["preferences", "schema_version", "modified"])


class Migration(migrations.Migration):
    dependencies = [
        ("crank", "0023_alter_agentrun_run_type_crawlrun"),
    ]

    operations = [
        migrations.RunPython(migrate_to_v2, reverse_migration),
    ]
