# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Post-deploy backfill for NULL ``last_crawl_at`` (issue #468).

The schema behind this command is split across migrations 0040-0045
(``0040_source_refresh_state`` .. ``0045_jobsourcecatalog_consecutive_failures``),
each holding exactly one schema-changing statement. None of them backfills
data: filling NULL ``last_crawl_at`` from each source's latest SUCCESS
``CrawlRun`` is done here so it can be re-run from any point after an
interruption, independent of Django's migration recorder.

Chunked by primary key (keyset pagination). Per chunk it issues one pk query
and one set-based ``UPDATE`` (with a correlated latest-success subquery);
dry-run issues one ``COUNT`` per chunk instead. There is no per-source query.
Idempotent: it only touches rows whose ``last_crawl_at`` is still NULL, so a
re-run after an interruption simply continues where it left off.

Run once, in a low-traffic window, after migration 0045 has been applied:

    python manage.py backfill_source_refresh_state --dry-run
    python manage.py backfill_source_refresh_state
"""

import argparse

from django.core.management.base import BaseCommand
from django.db import models

from crank.models import CrawlRun, JobSourceCatalog, SourceCatalog

DEFAULT_BATCH_SIZE = 500


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}")
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {number}")
    return number


_MODELS = (
    (JobSourceCatalog, "job_source"),
    (SourceCatalog, "source"),
)


def backfill_last_crawl_at(*, batch_size=DEFAULT_BATCH_SIZE, dry_run=False):
    """Fill NULL ``last_crawl_at`` from the latest SUCCESS crawl only.

    Returns a ``{model_label: rows_updated}`` dict. With ``dry_run=True`` no
    rows are written; the returned counts are what would be updated.
    """
    updated = {}
    for Model, field in _MODELS:
        count = 0
        last_pk = 0
        while True:
            chunk = list(
                Model.objects.filter(last_crawl_at__isnull=True, pk__gt=last_pk)
                .order_by("pk")
                .values_list("pk", flat=True)[:batch_size]
            )
            if not chunk:
                break
            last_pk = chunk[-1]
            latest_success = (
                CrawlRun.objects.filter(
                    **{field: models.OuterRef("pk")},
                    outcome="success",
                    finished_at__isnull=False,
                )
                .order_by("-finished_at")
                .values("finished_at")[:1]
            )
            rows = Model.objects.filter(
                pk__in=chunk, last_crawl_at__isnull=True
            ).filter(models.Exists(latest_success))
            if dry_run:
                count += rows.count()
            else:
                count += rows.update(last_crawl_at=models.Subquery(latest_success))
        updated[Model._meta.label] = count
    return updated


class Command(BaseCommand):
    help = (
        "Backfill NULL last_crawl_at from each source's latest SUCCESS "
        "CrawlRun (post-deploy step after migration 0045, issue #468). Idempotent and safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=_positive_int,
            default=DEFAULT_BATCH_SIZE,
            help=f"Rows per keyset chunk (default: {DEFAULT_BATCH_SIZE}).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Report rows that would be updated without writing.",
        )

    def handle(self, *args, **options):
        updated = backfill_last_crawl_at(
            batch_size=options["batch_size"], dry_run=options["dry_run"]
        )
        prefix = "[dry-run] would update" if options["dry_run"] else "updated"
        for label, count in updated.items():
            self.stdout.write(f"{prefix} {count} {label} rows")
        self.stdout.write(self.style.SUCCESS("backfill_source_refresh_state: done"))
        return 0
