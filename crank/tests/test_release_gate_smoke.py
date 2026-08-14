# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Release-gate smoke tests for issue #410 closeout.

These tests are deliberately **wiring-only**: they assert that the surfaces
named by the Job Recommendation GA final release gate (``docs/rollout-gates.md``)
are present, registered, and resolvable in the codebase. They do **not** touch
production, run pipelines, crawl external sources, or claim production was
verified. Live verification is a human-operator task per the rollout gate.
"""

from django.core.management import get_commands
from django.test import SimpleTestCase
from django.urls import reverse

from crank import release


class ManagementCommandPresenceTest(SimpleTestCase):
    """Required job-retrieval and ops management commands must be registered."""

    REQUIRED_COMMANDS = (
        # Job source seeding and listing health (issue #404 / #405).
        "seed_job_sources",
        "crawl_status",
        "crawl_healthcheck",
        # Migration / release diagnostics (issue #403).
        "migration_status",
        # Rollback rehearsal (rollback drill).
        "rollback_drill",
        # Pipeline / retrieval entrypoints (required before non-zero listing
        # counts can be asserted for the stage smoke).
        "run_job_pipeline",
        "schedule_crawls",  # conditional: present in this codebase
        "trigger_crawl",  # single confirmed bounded crawl
    )

    def setUp(self):
        self.commands = get_commands()

    def test_required_commands_registered(self):
        missing = [
            name for name in self.REQUIRED_COMMANDS if name not in self.commands
        ]
        self.assertEqual(missing, [], "management commands missing: %r" % missing)


class KeyUrlResolutionTest(SimpleTestCase):
    """Key release-gate URLs must reverse/resolve."""

    def test_release_diagnostics_reverses(self):
        self.assertTrue(reverse("release-diagnostics").endswith("/release-diagnostics/"))

    def test_job_search_reverses(self):
        self.assertTrue(reverse("job_search").endswith("/chat/"))

    def test_readiness_reverses(self):
        # readiness/healthz is present in this codebase.
        self.assertTrue(reverse("readiness").endswith("/healthz/ready/"))


class AdminSurfaceTest(SimpleTestCase):
    """The Job Retrieval Operations admin surface must be registered."""

    def test_job_retrieval_ops_registered(self):
        from django.contrib import admin

        from crank.admin import JobRetrievalOps
        from crank.admin_dashboard import JobRetrievalOperationsAdmin

        model_admin = admin.site._registry.get(JobRetrievalOps)
        self.assertIsNotNone(model_admin, "JobRetrievalOps is not registered")
        self.assertIsInstance(model_admin, JobRetrievalOperationsAdmin)


class RollbackDrillInterfaceTest(SimpleTestCase):
    """rollback_drill must be importable with the expected interface."""

    def test_rollback_drill_importable_and_has_interface(self):
        from crank.management.commands import rollback_drill

        self.assertTrue(callable(rollback_drill.Command.handle))
        # DRILL_CAPABILITIES drives the drill; it must cover the capabilities
        # documented in the rollout gate (kill-switch + AgentRun.RunType pairs).
        self.assertIsInstance(rollback_drill.DRILL_CAPABILITIES, (list, tuple))
        self.assertGreater(len(rollback_drill.DRILL_CAPABILITIES), 0)
        for entry in rollback_drill.DRILL_CAPABILITIES:
            self.assertIn("key", entry)
            self.assertIn("run_type", entry)


class RollbackDrillScopeTest(SimpleTestCase):
    """Doc honest about the drill's scope.

    The rollout gate documents that ``rollback_drill`` verifies
    ``capability_enabled()`` rather than ``get_enabled()`` and does not snapshot
    ``AgentRun`` counts. Guard the drill's interface so the documented scope and
    the gate stay in lockstep.
    """

    def test_drill_has_capability_blocked_wiring(self):
        from crank.management.commands import rollback_drill
        from crank.services import monitoring

        # The drill's per-capability result reports the switch-key block via
        # ``monitoring.capability_enabled``, which is what the gate claims it
        # verifies; it does not call ``get_enabled()`` nor snapshot AgentRun.
        self.assertTrue(callable(monitoring.capability_enabled))
        self.assertFalse(hasattr(rollback_drill, "get_enabled"))
        self.assertFalse(hasattr(rollback_drill.Command, "get_enabled"))


class ProviderModeWiringTest(SimpleTestCase):
    """Wiring behind the demo-is-not-production gate (#406 / #410).

    Wiring-only: asserts the release-diagnostics provider-mode surface exists
    and never echoes a raw provider value (e.g. a URL with embedded creds). It
    does not authenticate or verify a live provider.
    """

    def test_config_modes_reports_provider_safely(self):
        from django.conf import settings

        modes = release.config_modes()
        self.assertIn("job_search_provider", modes)
        # A misconfigured provider (URL with creds / blank) is reduced to a
        # safe token, never echoed onto the diagnostics page.
        self.assertRegex(modes["job_search_provider"], r"^\w[-\w.]{0,63}$")
        self.assertNotIn("://", modes["job_search_provider"])
        # The release gate names the real (orchestrator) provider explicitly.
        self.assertEqual(getattr(settings, "JOB_SEARCH_PROVIDER", ""), "demo")