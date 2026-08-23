# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for seed_job_sources management command."""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from crank.management.commands.seed_job_sources import SEED_SOURCES, _host, _is_allowed
from crank.models.job import JobSourceCatalog


class SeedJobSourcesTests(TestCase):
    """Idempotency, dry-run, allowlist enforcement, and seeding correctness."""

    def test_seed_creates_pending_disabled_rows(self):
        out = StringIO()
        call_command("seed_job_sources", stdout=out)
        created_count = JobSourceCatalog.objects.count()
        self.assertGreaterEqual(created_count, 1)
        for source in JobSourceCatalog.objects.all():
            self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.PENDING)
            self.assertFalse(source.enabled)
            host = _host(source.base_url)
            self.assertTrue(_is_allowed(host), f"{source.name} host {host} not allowlisted")

    def test_seed_is_idempotent(self):
        """Running twice does not duplicate rows."""
        call_command("seed_job_sources", stdout=StringIO())
        first_count = JobSourceCatalog.objects.count()
        call_command("seed_job_sources", stdout=StringIO())
        second_count = JobSourceCatalog.objects.count()
        self.assertEqual(first_count, second_count)

    def test_seed_preserves_operator_set_policy_fields(self):
        """Re-seeding must not clobber operator-set approval_state/enabled.

        The seed command creates rows as pending/disabled.  An operator then
        approves and enables a source through the admin UI.  Re-seeding must
        preserve those operator-set policy fields rather than silently
        resetting them to the seed defaults.
        """
        call_command("seed_job_sources", stdout=StringIO())
        source = JobSourceCatalog.objects.get(name=SEED_SOURCES[0]["name"])
        # Simulate operator approval and enablement
        source.approval_state = JobSourceCatalog.ApprovalState.APPROVED
        source.enabled = True
        source.save(update_fields=["approval_state", "enabled", "modified"])

        call_command("seed_job_sources", stdout=StringIO())
        source.refresh_from_db()
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.APPROVED)
        self.assertTrue(source.enabled)

    def test_seed_does_not_elevate_blocked_source(self):
        """Re-seeding must not change a blocked source to approved."""
        call_command("seed_job_sources", stdout=StringIO())
        source = JobSourceCatalog.objects.get(name=SEED_SOURCES[0]["name"])
        source.approval_state = JobSourceCatalog.ApprovalState.BLOCKED
        source.save(update_fields=["approval_state", "modified"])

        call_command("seed_job_sources", stdout=StringIO())
        source.refresh_from_db()
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.BLOCKED)
        self.assertFalse(source.enabled)

    def test_dry_run_does_not_write(self):
        out = StringIO()
        call_command("seed_job_sources", "--dry-run", stdout=out)
        self.assertEqual(JobSourceCatalog.objects.count(), 0)
        self.assertIn("[dry-run]", out.getvalue())

    def test_dry_run_reports_create_pending(self):
        out = StringIO()
        call_command("seed_job_sources", "--dry-run", stdout=out)
        self.assertIn("CREATE", out.getvalue())
        self.assertIn("pending", out.getvalue().lower())

    def test_dry_run_reports_no_change(self):
        """Second dry-run after seeding reports NO CHANGE for unchanged rows."""
        call_command("seed_job_sources", stdout=StringIO())
        out = StringIO()
        call_command("seed_job_sources", "--dry-run", stdout=out)
        self.assertIn("NO CHANGE", out.getvalue())

    def test_dry_run_reports_update_for_modified_row(self):
        call_command("seed_job_sources", stdout=StringIO())
        source = JobSourceCatalog.objects.first()
        source.adapter_key = "changed-adapter"
        source.save(update_fields=["adapter_key", "modified"])
        out = StringIO()
        call_command("seed_job_sources", "--dry-run", stdout=out)
        self.assertIn("UPDATE", out.getvalue())

    def test_all_seed_sources_use_allowlisted_domains(self):
        """Every curated seed source must have a base URL on the allowlist."""
        for entry in SEED_SOURCES:
            host = _host(entry["base_url"])
            self.assertTrue(
                _is_allowed(host),
                f"Seed source {entry['name']} has non-allowlisted host {host}",
            )

    def test_seed_source_names_are_unique(self):
        names = [entry["name"] for entry in SEED_SOURCES]
        self.assertEqual(len(names), len(set(names)))

    def test_seed_source_adapter_keys_are_valid(self):
        for entry in SEED_SOURCES:
            self.assertTrue(entry["adapter_key"])
            self.assertIsInstance(entry["adapter_key"], str)

    def test_skip_non_allowlisted_source(self):
        """A source whose host is not allowlisted is skipped with a warning."""
        from crank.management.commands import seed_job_sources

        original = list(seed_job_sources.SEED_SOURCES)
        seed_job_sources.SEED_SOURCES = [
            {
                "name": "Blocked Source",
                "adapter_key": "firecrawl-careers",
                "base_url": "https://evil.example.com/",
                "catalog_metadata": {},
            }
        ]
        try:
            out = StringIO()
            call_command("seed_job_sources", stdout=out)
            self.assertIn("SKIP", out.getvalue())
            self.assertEqual(JobSourceCatalog.objects.count(), 0)
        finally:
            seed_job_sources.SEED_SOURCES = original

    def test_diff_detects_adapter_key_change(self):
        call_command("seed_job_sources", stdout=StringIO())
        source = JobSourceCatalog.objects.first()
        out = StringIO()
        # Modify in DB so diff reports the change
        source.adapter_key = "changed-adapter"
        source.save(update_fields=["adapter_key", "modified"])
        call_command("seed_job_sources", "--dry-run", stdout=out)
        self.assertIn("adapter_key:", out.getvalue())

    def test_diff_detects_base_url_change(self):
        call_command("seed_job_sources", stdout=StringIO())
        source = JobSourceCatalog.objects.first()
        source.base_url = "https://jobs.example.test/changed"
        source.save(update_fields=["base_url", "modified"])
        out = StringIO()
        call_command("seed_job_sources", "--dry-run", stdout=out)
        self.assertIn("base_url:", out.getvalue())

    def test_diff_detects_catalog_metadata_change(self):
        call_command("seed_job_sources", stdout=StringIO())
        source = JobSourceCatalog.objects.first()
        source.catalog_metadata = {"new": "value"}
        source.save(update_fields=["catalog_metadata", "modified"])
        out = StringIO()
        call_command("seed_job_sources", "--dry-run", stdout=out)
        self.assertIn("catalog_metadata", out.getvalue())

    def test_diff_does_not_report_approval_or_enabled_changes(self):
        """The diff should not report approval_state or enabled changes because
        the seed command no longer modifies those fields on existing rows."""
        call_command("seed_job_sources", stdout=StringIO())
        source = JobSourceCatalog.objects.first()
        source.enabled = False
        source.approval_state = JobSourceCatalog.ApprovalState.PENDING
        source.save(update_fields=["enabled", "approval_state", "modified"])
        out = StringIO()
        call_command("seed_job_sources", "--dry-run", stdout=out)
        self.assertNotIn("approval_state:", out.getvalue())
        self.assertNotIn("enabled:", out.getvalue())

    def test_diff_detects_only_structural_changes(self):
        call_command("seed_job_sources", stdout=StringIO())
        source = JobSourceCatalog.objects.first()
        source.approval_state = JobSourceCatalog.ApprovalState.PENDING
        source.save(update_fields=["approval_state", "modified"])
        out = StringIO()
        call_command("seed_job_sources", "--dry-run", stdout=out)
        # approval_state is no longer reported by diff since the seed no
        # longer changes it on existing rows
        self.assertNotIn("approval_state:", out.getvalue())
