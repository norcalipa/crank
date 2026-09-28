# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Post-deploy backfill for NULL ``last_crawl_at`` (issue #468).

Migration ``0040_source_refresh_state`` is schema-only: it adds the
``last_attempt_at``/``consecutive_failures`` columns and tightens the
``last_crawl_at`` help text, and nothing else. Backfilling NULL
``last_crawl_at`` from each source's latest SUCCESS ``CrawlRun`` is done here
instead of in a migration ``RunPython`` step, so it can be re-run after a
partial failure without Django's "migration recorded only after every
operation succeeds" trap: on MySQL each schema operation auto-commits, so if
a data migration failed partway the migration would stay unapplied while the
columns already existed, and a rerun of ``migrate`` would fail on a duplicate
column. This command has no such constraint — run it again, from any point,
as many times as needed.

Chunked by primary key (keyset pagination, one aggregate query and at most
one update per chunk) so it never loads every pk or issues a query per
source. Idempotent: it only touches rows whose ``last_crawl_at`` is still
NULL, so a re-run after an interruption simply continues where it left off.

Run once, in a low-traffic window, after 0040 has been applied:

    python manage.py backfill_source_refresh_state --dry-run
    python manage.py backfill_source_refresh_state
"""

from django.core.management.base import BaseCommand
from django.db import models

from crank.models import CrawlRun, JobSourceCatalog, SourceCatalog

DEFAULT_BATCH_SIZE = 500

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
                if dry_run:
                    count += Model.objects.filter(
                        pk=row[field], last_crawl_at__isnull=True
                    ).count()
                else:
                    count += Model.objects.filter(
                        pk=row[field], last_crawl_at__isnull=True
                    ).update(last_crawl_at=row["latest"])
        updated[Model._meta.label] = count
    return updated


class Command(BaseCommand):
    help = (
        "Backfill NULL last_crawl_at from each source's latest SUCCESS "
        "CrawlRun (post-deploy step for migration 0040_source_refresh_state, "
        "issue #468). Idempotent and safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
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
