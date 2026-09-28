# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #468 (fair, bounded, truthful freshness scheduling)

from django.db import migrations, models


class Migration(migrations.Migration):
    # Schema-only: additive AddField/AlterField operations, nothing else.
    # The NULL last_crawl_at backfill lives in the
    # backfill_source_refresh_state management command (run post-deploy,
    # documented in docs/deployment-migrations.md and
    # docs/runbook-initial-crawl.md), not here. Django only records a
    # migration as applied after every one of its operations succeeds, and on
    # MySQL each successful AddField auto-commits regardless of `atomic`; a
    # RunPython backfill bundled into this migration could leave the columns
    # in place with 0040 unapplied after an interruption, and a rerun of
    # `migrate` would then fail on a duplicate column. Keeping 0040
    # schema-only and the backfill in an idempotent, separately-invoked
    # command avoids that trap entirely.

    dependencies = [
        ("crank", "0039_match_result_generation"),
    ]

    operations = [
        migrations.AddField(
            model_name="sourcecatalog",
            name="last_attempt_at",
            field=models.DateTimeField(blank=True, db_index=True, help_text="Last scheduled or manual attempt of any outcome; orders dispatch and retry backoff.", null=True),
        ),
        migrations.AddField(
            model_name="sourcecatalog",
            name="consecutive_failures",
            field=models.PositiveIntegerField(default=0, help_text="Attempts since the last success that did not fully succeed."),
        ),
        migrations.AddField(
            model_name="jobsourcecatalog",
            name="last_attempt_at",
            field=models.DateTimeField(blank=True, db_index=True, help_text="Last scheduled or manual attempt of any outcome; orders dispatch and retry backoff.", null=True),
        ),
        migrations.AddField(
            model_name="jobsourcecatalog",
            name="consecutive_failures",
            field=models.PositiveIntegerField(default=0, help_text="Attempts since the last success that did not fully succeed."),
        ),
        migrations.AlterField(
            model_name="sourcecatalog",
            name="last_crawl_at",
            field=models.DateTimeField(blank=True, db_index=True, help_text="Last SUCCESSFUL fetch. Never advanced by partial or failed attempts.", null=True),
        ),
        migrations.AlterField(
            model_name="jobsourcecatalog",
            name="last_crawl_at",
            field=models.DateTimeField(blank=True, db_index=True, help_text="Last SUCCESSFUL fetch. Never advanced by partial or failed attempts.", null=True),
        ),
    ]
