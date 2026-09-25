# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #468 (fair, bounded, truthful freshness scheduling)

from django.db import migrations, models


BACKFILL_CHUNK = 500


def backfill_last_crawl_at(apps, schema_editor):
    """Fill NULL ``last_crawl_at`` from the latest SUCCESS manual crawl only.

    Chunked by primary key (keyset pagination, one aggregate query and at most
    one update per row) so it never loads every pk or issues a query per
    source. Resumable and idempotent: it only touches rows whose
    ``last_crawl_at`` is still NULL, so a re-run after an interruption simply
    continues.
    """
    CrawlRun = apps.get_model("crank", "CrawlRun")
    for model_name, field in (("JobSourceCatalog", "job_source"), ("SourceCatalog", "source")):
        Model = apps.get_model("crank", model_name)
        last_pk = 0
        while True:
            chunk = list(
                Model.objects.filter(last_crawl_at__isnull=True, pk__gt=last_pk)
                .order_by("pk")
                .values_list("pk", flat=True)[:BACKFILL_CHUNK]
            )
            if not chunk:
                break
            last_pk = chunk[-1]
            latest = (
                CrawlRun.objects.filter(
                    **{f"{field}__in": chunk},
                    outcome="success",
                    finished_at__isnull=False,
                )
                .values(field)
                .annotate(latest=models.Max("finished_at"))
            )
            for row in latest:
                Model.objects.filter(pk=row[field], last_crawl_at__isnull=True).update(
                    last_crawl_at=row["latest"]
                )


class Migration(migrations.Migration):
    # MySQL DDL is non-transactional and auto-commits; keeping the migration
    # non-atomic makes the schema steps and the chunked backfill independently
    # resumable instead of pretending a single rollback boundary exists.
    atomic = False

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
