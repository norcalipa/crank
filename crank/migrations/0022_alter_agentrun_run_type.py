# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("crank", "0021_crawl_freshness")]
    operations = [
        migrations.AlterField(
            model_name="agentrun",
            name="run_type",
            field=models.CharField(
                choices=[
                    ("noop", "No-op reference run"),
                    ("gather_scores", "Score gathering run"),
                    ("job_pipeline", "Job pipeline run"),
                    ("crawl_schedule", "Crawl scheduling run"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
    ]
