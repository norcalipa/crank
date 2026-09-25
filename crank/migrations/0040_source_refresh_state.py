# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #468 (fair, bounded, truthful freshness scheduling)

from django.db import migrations, models


def backfill_last_crawl_at(apps, schema_editor):
    """Fill NULL ``last_crawl_at`` from the latest SUCCESS manual crawl only."""
    CrawlRun = apps.get_model("crank", "CrawlRun")
    for model_name, field in (("JobSourceCatalog", "job_source"), ("SourceCatalog", "source")):
        Model = apps.get_model("crank", model_name)
        for pk in Model.objects.filter(last_crawl_at__isnull=True).values_list("pk", flat=True):
            finished = (
                CrawlRun.objects.filter(
                    **{field: pk},
                    outcome="success",
                    finished_at__isnull=False,
                )
                .order_by("-finished_at")
                .values_list("finished_at", flat=True)
                .first()
            )
            if finished is not None:
                Model.objects.filter(pk=pk, last_crawl_at__isnull=True).update(
                    last_crawl_at=finished
                )


class Migration(migrations.Migration):

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
        migrations.RunPython(backfill_last_crawl_at, migrations.RunPython.noop),
    ]
