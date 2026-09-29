# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #468 (fair, bounded, truthful freshness scheduling)

from django.db import migrations, models


class Migration(migrations.Migration):
    # Exactly one schema-changing statement on MySQL (ADD COLUMN sourcecatalog.consecutive_failures); the
    # migration recorder commits after it. db_default keeps the column default
    # in the database, so Django does not follow the ADD COLUMN with a second
    # `ALTER COLUMN ... DROP DEFAULT` statement. MySQL commits each DDL
    # statement implicitly and Django records the migration only afterwards;
    # that residual window is inherent to Django on MySQL and is covered by
    # the recovery steps in docs/deployment-migrations.md (verify the
    # column/index, then `migrate --fake` that migration). The NULL
    # last_crawl_at backfill lives in the backfill_source_refresh_state
    # management command (post-deploy), not in a migration.

    dependencies = [
        ("crank", "0041_sourcecatalog_last_attempt_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="sourcecatalog",
            name="consecutive_failures",
            field=models.PositiveIntegerField(db_default=0, default=0, help_text="Attempts since the last success that did not fully succeed."),
        ),
    ]
