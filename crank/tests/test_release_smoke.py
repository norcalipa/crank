# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Post-deploy smoke checks for issue #403 acceptance criteria.

Covers the surfaces the acceptance criteria name: the Django admin, the
job-search page, migration health, and static-asset fingerprints. The stale
asset detection itself (``release_build_status``) is unit-tested in
``test_release.py``; here we verify the integration shape surfaces through the
public ``diagnostics()`` payload.
"""

import os
import shutil
import tempfile

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from crank.release import diagnostics, frontend_build_id, migration_status_summary


class MigrationHealthSmokeTest(TestCase):
    def test_test_database_is_fully_migrated(self):
        # The test database is migrated before the suite runs, so the
        # diagnostics must report a clean, zero-pending migration graph.
        summary = migration_status_summary()
        self.assertEqual(summary["status"], "clean")
        self.assertEqual(summary["pending_count"], 0)


class AdminAndJobSearchSmokeTest(TestCase):
    """``/admin/`` and the job-search page must both serve to staff."""

    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.client.force_login(self.staff)
        # ``base.html`` renders ``{% manifest 'main.js' %}``, which reads the
        # webpack manifest from STATICFILES_DIRS. Point it at a hermetic
        # fixture so the pages render without a webpack build in the test env.
        self.manifest_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.manifest_dir, ignore_errors=True)
        with open(
            os.path.join(self.manifest_dir, "manifest.json"), "w", encoding="utf-8"
        ) as handle:
            handle.write('{"main.js": "main.deadbeefcafe.js"}')
        self._manifest_settings = override_settings(
            STATICFILES_DIRS=[self.manifest_dir]
        )
        self._manifest_settings.enable()

    def tearDown(self):
        self._manifest_settings.disable()
        super().tearDown()

    def test_admin_index_loads_for_staff(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)

    def test_job_search_page_loads_for_staff(self):
        response = self.client.get(reverse("job_search"))
        self.assertEqual(response.status_code, 200)

    def test_release_diagnostics_loads_for_staff(self):
        response = self.client.get(reverse("release-diagnostics"))
        self.assertEqual(response.status_code, 200)


class StaticAssetFingerprintSmokeTest(TestCase):
    def setUp(self):
        # ``diagnostics()`` caches full-table COUNT(*) via ``counts()``;
        # clear so the shape assertion is unaffected by prior cached data.
        cache.clear()

    def test_frontend_build_id_is_a_contenthash(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, "manifest.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"main.js": "main.deadbeefcafe.js"}')
        with override_settings(MANIFEST_LOADER={"MANIFEST_PATH": path}):
            self.assertEqual(frontend_build_id(), "deadbeefcafe")

    def test_diagnostics_surfaces_build_status(self):
        data = diagnostics()
        self.assertIn("build", data)
        self.assertEqual(set(data["build"]), {"status", "mismatched"})
        self.assertIn(data["build"]["status"], {"ok", "mismatch", "unverifiable"})
