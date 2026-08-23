# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the application navigation shell (issue #443).

Covers anonymous/authenticated/admin visibility, active route state with
aria-current, skip-to-content link, semantic navigation landmarks, CSRF-safe
logout, and mobile drawer markup.
"""
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.contrib.auth.models import User
from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site
from django.core.cache import cache


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    SECRET_KEY="test-secret",
)
class NavigationShellTests(TestCase):
    """Application shell navigation tests for issue #443."""

    def setUp(self):
        cache.clear()
        self.client = Client()
        social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="test",
            secret="test",
        )
        social_app.sites.add(Site.objects.get_current())

    def _create_user(self, is_staff=False):
        user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
            is_staff=is_staff,
        )
        return user

    # --- Skip-to-content link -------------------------------------------

    def test_skip_to_content_link_present(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, "Skip to content")
        self.assertContains(response, 'href="#main-content"')
        self.assertContains(response, "skip-to-content")

    def test_main_content_has_id_and_tabindex(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, 'id="main-content"')
        self.assertContains(response, 'tabindex="-1"')

    # --- Semantic landmarks ---------------------------------------------

    def test_navigation_has_semantic_landmark(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, 'aria-label="Application navigation"')
        self.assertContains(response, 'aria-label="Main navigation"')
        self.assertContains(response, '<nav ')

    # --- Anonymous visibility -------------------------------------------

    def test_anonymous_sees_rankings_job_search_help_login(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, "Company Rankings")
        self.assertContains(response, "Job Search")
        self.assertContains(response, "Help")
        self.assertContains(response, "Login")
        self.assertNotContains(response, "Logout")
        self.assertNotContains(response, "Admin")

    def test_anonymous_does_not_see_admin_link(self):
        response = self.client.get(reverse("index"))
        self.assertNotContains(response, 'id="nav-admin"')

    # --- Authenticated visibility --------------------------------------

    def test_authenticated_sees_logout_and_username(self):
        user = self._create_user()
        self.client.force_login(user)
        response = self.client.get(reverse("index"))
        self.assertContains(response, "Logout")
        self.assertContains(response, "testuser")
        self.assertContains(response, 'id="nav-account"')

    def test_authenticated_non_staff_sees_admin_link(self):
        """Authenticated users see Admin per issue requirement (authorization
        remains server-side — hiding a link is not authorization)."""
        user = self._create_user(is_staff=False)
        self.client.force_login(user)
        response = self.client.get(reverse("index"))
        self.assertContains(response, 'id="nav-admin"')

    def test_staff_sees_admin_link(self):
        user = self._create_user(is_staff=True)
        self.client.force_login(user)
        response = self.client.get(reverse("index"))
        self.assertContains(response, 'id="nav-admin"')
        self.assertContains(response, "Admin")

    # --- Active route state ---------------------------------------------

    def test_rankings_active_on_index(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, 'aria-current="page"')
        self.assertContains(response, 'app-nav-link--active')

    def test_job_search_active_on_chat(self):
        user = self._create_user()
        self.client.force_login(user)
        response = self.client.get(reverse("job_search"))
        self.assertContains(response, 'aria-current="page"')
        # Job Search nav link should be active
        self.assertInHTML(
            '<span class="app-nav-label">Job Search</span>',
            response.content.decode(),
        )

    def test_help_active_on_help(self):
        response = self.client.get(reverse("help"))
        self.assertContains(response, 'aria-current="page"')

    # --- CSRF-safe logout -----------------------------------------------

    def test_logout_form_has_csrf_token(self):
        user = self._create_user()
        self.client.force_login(user)
        response = self.client.get(reverse("index"))
        self.assertContains(response, 'action="/accounts/logout/"')
        self.assertContains(response, "csrfmiddlewaretoken")

    # --- Mobile drawer markup -------------------------------------------

    def test_mobile_drawer_toggle_present(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, "app-nav-toggle")
        self.assertContains(response, 'aria-expanded="false"')
        self.assertContains(response, 'aria-controls="mobile-nav"')

    def test_mobile_drawer_has_close_button(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, "app-nav-drawer-close")
        self.assertContains(response, 'aria-label="Close navigation menu"')

    def test_mobile_drawer_has_overlay(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, "app-nav-overlay")

    # --- Navigation JS reference ----------------------------------------

    def test_nav_js_loaded(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, "app-nav.js")

    # --- Brand/home affordance ------------------------------------------

    def test_logo_links_to_home(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, 'aria-label="CRank home"')

    def test_rankings_is_explicit_nav_destination(self):
        """Company Rankings must be an explicit nav link, not just the logo."""
        response = self.client.get(reverse("index"))
        self.assertContains(response, 'id="nav-rankings"')
        self.assertContains(response, "Company Rankings")

    # --- Route centralization -------------------------------------------

    def test_context_processor_provides_nav_items(self):
        from crank.context_processors import navigation_context
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        ctx = navigation_context(request)
        self.assertIn("nav_items", ctx)
        labels = [item["label"] for item in ctx["nav_items"]]
        self.assertIn("Company Rankings", labels)
        self.assertIn("Job Search", labels)
        self.assertIn("Help", labels)

    def test_context_processor_active_states(self):
        from crank.context_processors import navigation_context
        from django.test import RequestFactory

        factory = RequestFactory()

        request = factory.get("/")
        ctx = navigation_context(request)
        rankings = [i for i in ctx["nav_items"] if i["label"] == "Company Rankings"][0]
        self.assertTrue(rankings["is_active"])

        request = factory.get("/chat/")
        ctx = navigation_context(request)
        job_search = [i for i in ctx["nav_items"] if i["label"] == "Job Search"][0]
        self.assertTrue(job_search["is_active"])

        request = factory.get("/help/")
        ctx = navigation_context(request)
        help_item = [i for i in ctx["nav_items"] if i["label"] == "Help"][0]
        self.assertTrue(help_item["is_active"])

    # --- No horizontal scroll at 320px ----------------------------------

    def test_css_has_overflow_x_hidden(self):
        import os
        css_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
            "static", "css", "popup.css",
        )
        with open(css_path) as f:
            css = f.read()
        self.assertIn("overflow-x: hidden", css)

    # --- Reduced motion support -----------------------------------------

    def test_css_has_reduced_motion_support(self):
        import os
        css_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
            "static", "css", "popup.css",
        )
        with open(css_path) as f:
            css = f.read()
        self.assertIn("prefers-reduced-motion", css)
