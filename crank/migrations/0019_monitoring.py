# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django_extensions.db.fields


class Migration(migrations.Migration):
    dependencies = [
        ("crank", "0018_alter_agentrun_run_type"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CapabilitySwitch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", django_extensions.db.fields.CreationDateTimeField(auto_now_add=True, verbose_name="created")),
                ("modified", django_extensions.db.fields.ModificationDateTimeField(auto_now=True, verbose_name="modified")),
                ("key", models.CharField(max_length=64, unique=True)),
                ("enabled", models.BooleanField(db_index=True, default=True)),
                ("note", models.CharField(blank=True, default="", max_length=200)),
            ],
            options={"ordering": ["key"]},
        ),
        migrations.CreateModel(
            name="OperationalChangeAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", django_extensions.db.fields.CreationDateTimeField(auto_now_add=True, verbose_name="created")),
                ("modified", django_extensions.db.fields.ModificationDateTimeField(auto_now=True, verbose_name="modified")),
                ("target_type", models.CharField(max_length=32)),
                ("target_id", models.CharField(max_length=64)),
                ("action", models.CharField(max_length=32)),
                ("old_value", models.JSONField(blank=True, default=dict)),
                ("new_value", models.JSONField(blank=True, default=dict)),
                ("confirmed", models.BooleanField(default=False)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="operational_change_audits", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created", "-id"]},
        ),
        migrations.AddIndex(
            model_name="operationalchangeaudit",
            index=models.Index(fields=["target_type", "target_id"], name="crank_opaudit_target_idx"),
        ),
        migrations.AddIndex(
            model_name="operationalchangeaudit",
            index=models.Index(fields=["action"], name="crank_opaudit_action_idx"),
        ),
    ]
