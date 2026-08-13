# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crank", "0023_alter_agentrun_run_type_crawlrun"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobsearchmessage",
            name="results_json",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Bounded JSON of citation-validated structured results "
                    "(job/org cards), persisted so history renders identically "
                    "on reload."
                ),
            ),
        ),
    ]
