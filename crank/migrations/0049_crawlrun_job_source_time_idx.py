# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #535 (index for the per-source crawl-run history)

from django.db import migrations, models


class Migration(migrations.Migration):
    # Exactly one schema-changing statement on MySQL (CREATE INDEX on
    # crank_crawlrun). MySQL commits each DDL statement implicitly and Django
    # records the migration only afterwards; that residual window is covered
    # by the recovery steps in docs/deployment-migrations.md (verify the
    # index, then `migrate --fake` that migration).

    dependencies = [
        ("crank", "0048_jobsearchconversation_first_result_at"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="crawlrun",
            index=models.Index(fields=["job_source", "-started_at", "-id"], name="crank_crawl_jobsrc_time_idx"),
        ),
    ]
