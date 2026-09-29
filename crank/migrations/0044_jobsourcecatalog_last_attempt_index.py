# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #468 (fair, bounded, truthful freshness scheduling)

from django.db import migrations, models


class Migration(migrations.Migration):
    # Exactly one schema-changing statement on MySQL (CREATE INDEX on jobsourcecatalog.last_attempt_at); the
    # migration recorder commits after it, so an interrupted `migrate` leaves
    # only whole, recorded steps and a rerun resumes at the next migration.
    # MySQL DDL auto-commits, so a single migration with several statements
    # can still be left half-applied and unrecorded; this split is what
    # prevents a duplicate-column failure on rerun. The NULL last_crawl_at
    # backfill lives in the backfill_source_refresh_state management
    # command (post-deploy), not in a migration.

    dependencies = [
        ("crank", "0043_jobsourcecatalog_last_attempt_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="jobsourcecatalog",
            name="last_attempt_at",
            field=models.DateTimeField(blank=True, db_index=True, help_text="Last scheduled or manual attempt of any outcome; orders dispatch and retry backoff.", null=True),
        ),
    ]
