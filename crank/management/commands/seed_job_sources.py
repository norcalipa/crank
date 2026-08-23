# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Seed catalog JobSourceCatalog rows for an initial curated set.

The command is idempotent: re-running updates structural fields (adapter
key, base URL, catalog metadata) without duplicating.  Crucially, it does
**not** change ``approval_state`` or ``enabled`` on existing rows — those
are operator-controlled policy fields that can only be changed through the
admin UI or an explicit management action.  New rows are created with
``pending`` approval and ``enabled=False`` so the seed never silently
elevates a source to live traffic.

A dry-run mode prints what would change without touching the database.

Only domains on the code-owned ``APPROVED_JOB_SOURCE_DOMAINS`` allowlist are
seeded, so the SSRF guard is preserved.  Sources whose base URL host is not
allowlisted are skipped with a warning.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.core.management.base import BaseCommand
from django.db import transaction

from crank.agents.jobs.base import APPROVED_JOB_SOURCE_DOMAINS
from crank.models.job import JobSourceCatalog

#: Curated initial seed sources.  Each entry maps a catalog name to an
#: adapter key, base URL, and optional catalog metadata.  Only domains on
#: the code-owned allowlist are included; the list is intentionally short
#: for the first production crawl.
SEED_SOURCES: list[dict[str, object]] = [
    {
        "name": "USAJOBS Search",
        "adapter_key": "usajobs",
        "base_url": "https://data.usajobs.gov/",
        "catalog_metadata": {
            "description": "USA federal job postings via the official Search API.",
            "canonical_host": "www.usajobs.gov",
        },
    },
    {
        "name": "Remote OK",
        "adapter_key": "firecrawl-careers",
        "base_url": "https://remoteok.com/",
        "catalog_metadata": {
            "description": "Remote job listings via Firecrawl extraction.",
        },
    },
    {
        "name": "Greenhouse Job Board",
        "adapter_key": "firecrawl-careers",
        "base_url": "https://boards-api.greenhouse.io/",
        "catalog_metadata": {
            "description": "Greenhouse ATS job board API via Firecrawl extraction.",
        },
    },
    {
        "name": "Lever Postings",
        "adapter_key": "firecrawl-careers",
        "base_url": "https://api.lever.co/",
        "catalog_metadata": {
            "description": "Lever postings API via Firecrawl extraction.",
        },
    },
]


def _host(base_url: str) -> str:
    return (urlsplit(base_url).hostname or "").lower().rstrip(".")


def _is_allowed(host: str) -> bool:
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in APPROVED_JOB_SOURCE_DOMAINS
    )


class Command(BaseCommand):
    help = "Seed catalog JobSourceCatalog rows for the initial crawl (does not elevate pending/blocked sources)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created/updated without writing to the database.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        created = 0
        updated = 0
        skipped = 0

        for entry in SEED_SOURCES:
            name = entry["name"]
            adapter_key = entry["adapter_key"]
            base_url = entry["base_url"]
            metadata = entry.get("catalog_metadata", {})

            host = _host(base_url)
            if not _is_allowed(host):
                self.stdout.write(
                    self.style.WARNING(
                        f"SKIP {name}: host {host!r} is not on the code-owned allowlist"
                    )
                )
                skipped += 1
                continue

            if dry_run:
                existing = JobSourceCatalog.objects.filter(name=name).first()
                if existing is None:
                    self.stdout.write(
                        self.style.NOTICE(f"CREATE {name} ({adapter_key}) -> {base_url} [pending, disabled]")
                    )
                    created += 1
                else:
                    changes = self._diff(existing, adapter_key, base_url, metadata)
                    if changes:
                        self.stdout.write(
                            self.style.NOTICE(f"UPDATE {name}: {', '.join(changes)}")
                        )
                        updated += 1
                    else:
                        self.stdout.write(
                            self.style.NOTICE(f"NO CHANGE {name}")
                        )
                continue

            _obj, created_flag = JobSourceCatalog.objects.get_or_create(
                name=name,
                defaults={
                    "adapter_key": adapter_key,
                    "base_url": base_url,
                    # New rows start pending and disabled.  An operator must
                    # explicitly approve and enable through the admin UI.
                    "approval_state": JobSourceCatalog.ApprovalState.PENDING,
                    "enabled": False,
                    "catalog_metadata": metadata,
                },
            )
            if not created_flag:
                # Update structural fields only; preserve operator-set
                # approval_state and enabled on existing rows.
                _obj.adapter_key = adapter_key
                _obj.base_url = base_url
                _obj.catalog_metadata = metadata
                _obj.save(update_fields=["adapter_key", "base_url", "catalog_metadata", "modified"])
            if created_flag:
                created += 1
                self.stdout.write(
                    self.style.SUCCESS(f"CREATED {name} ({adapter_key}) [pending, disabled]")
                )
            else:
                updated += 1
                self.stdout.write(
                    self.style.SUCCESS(f"UPDATED {name} ({adapter_key}) [policy preserved]")
                )

        summary = f"seed_job_sources: {created} created, {updated} updated, {skipped} skipped"
        if dry_run:
            summary = f"[dry-run] {summary}"
        self.stdout.write(self.style.SUCCESS(summary))
        return 0

    @staticmethod
    def _diff(existing, adapter_key, base_url, metadata):
        changes = []
        if existing.adapter_key != adapter_key:
            changes.append(f"adapter_key: {existing.adapter_key} -> {adapter_key}")
        if existing.base_url != base_url:
            changes.append(f"base_url: {existing.base_url} -> {base_url}")
        if existing.catalog_metadata != metadata:
            changes.append("catalog_metadata updated")
        return changes
