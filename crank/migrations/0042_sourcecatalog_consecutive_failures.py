# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #468 (fair, bounded, truthful freshness scheduling)

from django.db import migrations, models


class Migration(migrations.Migration):
    # Exactly one schema-changing statement on MySQL (ADD COLUMN sourcecatalog.consecutive_failures); the
    # migration recorder commits after it, so an interrupted `migrate` leaves
    # only whole, recorded steps and a rerun resumes at the next migration.
    # MySQL DDL auto-commits, so a single migration with several statements
    # can still be left half-applied and unrecorded; this split is what
    # prevents a duplicate-column failure on rerun. The NULL last_crawl_at
    # backfill lives in the backfill_source_refresh_state management
    # command (post-deploy), not in a migration.

    dependencies = [
        ("crank", "0041_sourcecatalog_last_attempt_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="sourcecatalog",
            name="consecutive_failures",
            field=models.PositiveIntegerField(default=0, help_text="Attempts since the last success that did not fully succeed."),
        ),
    ]
