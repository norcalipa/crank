# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Persist turn delivery state on job-search chat messages (issue #458).

Additive, nullable columns so legacy rows (NULL) derive their delivery state
at read time from the presence of a matching assistant reply. No backfill:
runtime derivation keeps legacy semantics identical to pre-#458 behavior and
the change stays reversible.

MySQL note: no partial/ex conditional unique indexes are introduced here —
MySQL does not support them ( warning W036 ). Deduplication continues to rely
on the existing ``unique_jobsearch_message_idempotency`` constraint, which is
a plain composite unique constraint over (conversation, idempotency_key, role)
and is valid on both MySQL and SQLite.
"""
from django.db import migrations, models

from crank.models.job_search import JobSearchMessage


class Migration(migrations.Migration):

    dependencies = [
        ("crank", "0029_merge_20260815_1645"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobsearchmessage",
            name="delivery_state",
            field=models.CharField(
                blank=True,
                choices=JobSearchMessage.DeliveryState.choices,
                db_index=True,
                default="",
                max_length=16,
                help_text="Delivery state of a user turn: pending (in flight), completed "
                          "(assistant reply exists), or failed. Rows written before this "
                          "column existed are NULL and derive their state at read time "
                          "from the presence of a matching assistant reply.",
            ),
        ),
        migrations.AddField(
            model_name="jobsearchmessage",
            name="failure_code",
            field=models.CharField(
                blank=True,
                choices=JobSearchMessage.FailureCode.choices,
                default="",
                max_length=32,
                help_text="Stable failure code recorded when a user turn ends failed; "
                          "matches the typed error envelope's ``error.type``.",
            ),
        ),
    ]
