# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #468 (fair, bounded, truthful freshness scheduling)

from django.db import migrations, models


class Migration(migrations.Migration):
    # Exactly one schema-changing statement on MySQL (CREATE INDEX on sourcecatalog.last_attempt_at); the
    # migration recorder commits after it. MySQL commits each DDL
    # statement implicitly and Django records the migration only afterwards;
    # that residual window is inherent to Django on MySQL and is covered by
    # the recovery steps in docs/deployment-migrations.md (verify the
    # column/index, then `migrate --fake` that migration). The NULL
    # last_crawl_at backfill lives in the backfill_source_refresh_state
    # management command (post-deploy), not in a migration.

    dependencies = [
        ("crank", "0040_source_refresh_state"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sourcecatalog",
            name="last_attempt_at",
            field=models.DateTimeField(blank=True, db_index=True, help_text="Last scheduled or manual attempt of any outcome; orders dispatch and retry backoff.", null=True),
        ),
    ]
