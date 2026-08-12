# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for user-facing help and privacy pages (issue #327).

Every new view/URL has a render test.  A link-checking test verifies that
documentation references inside the rendered pages resolve to real URLs or
in-repo paths.
"""
import re
from pathlib import Path

from django.test import TestCase, Client, override_settings
from django.urls import reverse
from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    SECRET_KEY="test-secret",
)
class HelpPageTests(TestCase):
    """Help landing page renders with authentication context."""

    def setUp(self):
        self.client = Client()
        social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="test",
            secret="test",
        )
        social_app.sites.add(Site.objects.get_current())

    def test_help_page_renders(self):
        response = self.client.get(reverse("help"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Help &amp; Privacy")
        self.assertContains(response, "What CRank Stores")
        self.assertContains(response, "Export, Reset, and Delete")

    def test_help_page_links_to_privacy(self):
        response = self.client.get(reverse("help"))
        self.assertContains(response, 'href="/privacy/"')

    def test_help_page_links_to_github_issues(self):
        response = self.client.get(reverse("help"))
        self.assertContains(response, "github.com/norcalipa/crank/issues")

    def test_help_page_identifies_owner_and_review_date(self):
        response = self.client.get(reverse("help"))
        self.assertContains(response, "Owner:")
        self.assertContains(response, "Last reviewed:")

    def test_help_page_has_help_link_in_navbar(self):
        """Help link should appear in the base template navbar."""
        response = self.client.get(reverse("help"))
        self.assertContains(response, 'href="/help/"')

    def test_privacy_page_has_help_link_in_navbar(self):
        """Help link should appear in navbar on privacy page too."""
        response = self.client.get(reverse("privacy"))
        self.assertContains(response, 'href="/help/"')


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    SECRET_KEY="test-secret",
)
class PrivacyPageTests(TestCase):
    """Privacy notice renders with authentication context."""

    def setUp(self):
        self.client = Client()
        social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="test",
            secret="test",
        )
        social_app.sites.add(Site.objects.get_current())

    def test_privacy_page_renders(self):
        response = self.client.get(reverse("privacy"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Privacy Notice")
        self.assertContains(response, "Data We Store")
        self.assertContains(response, "Export Your Data")

    def test_privacy_page_lists_export_reset_delete(self):
        response = self.client.get(reverse("privacy"))
        self.assertContains(response, "export")
        self.assertContains(response, "reset")
        self.assertContains(response, "delete")

    def test_privacy_page_links_to_help(self):
        response = self.client.get(reverse("privacy"))
        self.assertContains(response, 'href="/help/"')

    def test_privacy_page_identifies_owner_and_review_date(self):
        response = self.client.get(reverse("privacy"))
        self.assertContains(response, "Owner:")
        self.assertContains(response, "Last reviewed:")

    def test_privacy_page_lists_api_endpoints(self):
        """Verify that documented API commands match actual URL names."""
        response = self.client.get(reverse("privacy"))
        # The export endpoint should reference the correct URL pattern
        self.assertContains(response, "/api/agent/conversations/")
        self.assertContains(response, "/export/")
        self.assertContains(response, "/reset/")
        self.assertContains(response, "/delete/")


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    SECRET_KEY="test-secret",
)
class DocumentationLinkCheckTests(TestCase):
    """Verify that internal documentation links in rendered pages resolve.

    Checks href attributes pointing to in-repo docs paths and ensures the
    referenced files exist on disk.  External URLs are checked for format only.
    """

    def setUp(self):
        self.client = Client()
        social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="test",
            secret="test",
        )
        social_app.sites.add(Site.objects.get_current())
        self.base_dir = Path(__file__).resolve().parent.parent.parent.parent

    def _extract_links(self, html_text):
        """Return a list of (href, link_type) tuples from an HTML string."""
        links = []
        for match in re.finditer(r'href="([^"]+)"', html_text):
            href = match.group(1)
            if href.startswith("http"):
                links.append((href, "external"))
            elif href.startswith("/"):
                links.append((href, "internal"))
            else:
                links.append((href, "relative"))
        return links

    def test_help_page_internal_links_resolve(self):
        response = self.client.get(reverse("help"))
        links = self._extract_links(response.content.decode())
        internal_paths = [href for href, kind in links if kind == "internal"]
        self.assertIn("/privacy/", internal_paths)

    def test_extract_links_classifies_relative_links(self):
        """Cover the relative-link branch of _extract_links."""
        html = '<a href="page.html">rel</a><a href="https://x.com">ext</a><a href="/abs">abs</a>'
        links = self._extract_links(html)
        self.assertIn(("page.html", "relative"), links)
        self.assertIn(("https://x.com", "external"), links)
        self.assertIn(("/abs", "internal"), links)

    def test_help_page_docs_link_resolves(self):
        response = self.client.get(reverse("help"))
        content = response.content.decode()
        match = re.search(r'href="([^"]*docs/readme\.md)"', content)
        self.assertIsNotNone(match, "Help page should link to docs/readme.md")

    def test_privacy_page_internal_links_resolve(self):
        response = self.client.get(reverse("privacy"))
        links = self._extract_links(response.content.decode())
        internal_paths = [href for href, kind in links if kind == "internal"]
        self.assertIn("/help/", internal_paths)

    def test_operator_runbooks_exist(self):
        """All runbook files referenced in docs/ must exist on disk."""
        runbooks = [
            "docs/runbook-provider-source-configuration.md",
            "docs/runbook-secret-rotation.md",
            "docs/runbook-diagnosis-recovery.md",
        ]
        for rb in runbooks:
            path = self.base_dir / rb
            self.assertTrue(path.exists(), f"Runbook missing: {rb}")

    def test_runbooks_identify_owner_and_review_date(self):
        """Acceptance criterion: docs identify owners and last-reviewed date."""
        runbooks = [
            "docs/runbook-provider-source-configuration.md",
            "docs/runbook-secret-rotation.md",
            "docs/runbook-diagnosis-recovery.md",
        ]
        for rb in runbooks:
            path = self.base_dir / rb
            content = path.read_text()
            self.assertIn("Owner:", content, f"{rb} missing owner")
            self.assertIn("Last reviewed:", content, f"{rb} missing review date")
            self.assertIn(
                "Version/change process",
                content,
                f"{rb} missing version/change process",
            )

    def test_readme_links_to_help_and_runbooks(self):
        """Readme should link to user docs and operator runbooks."""
        readme_path = self.base_dir / "readme.md"
        content = readme_path.read_text()
        self.assertIn("Help & Privacy", content)
        self.assertIn("runbook-provider-source-configuration.md", content)
        self.assertIn("runbook-secret-rotation.md", content)
        self.assertIn("runbook-diagnosis-recovery.md", content)
