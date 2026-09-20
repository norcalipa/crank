# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
Tests for the advisory, user-safe assistant-status endpoint (issue #457).

Covers: every state produced by its defining condition, classification
precedence, bounded-envelope leak assertions (no provider names, no
capability issue strings, no secret-presence flags), cache TTL and
auth-state keying, anonymous/authenticated/staff envelope parity, HTTP
semantics, and the advisory-only contract (the GET path performs no
preference or conversation writes).
"""
import json
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crank.agents.job_search.demo import AssistantUnavailable
from crank.models import AgentRun, JobListing, JobSourceCatalog, JobSearchConversation


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

READY_SETTINGS = {
    "INTERACTIVE_AGENT_ENABLED": True,
    "JOB_SEARCH_PROVIDER": "demo",
    "ENV": "dev",
}

# Strings that must never appear in the public envelope, whatever the
# internal failure looks like (issue #457 AC-1/AC-5).
FORBIDDEN_STRINGS = (
    "LLM_PROVIDER",
    "LLM_API_KEY",
    "LLM_MODEL",
    "OrchestratorJobSearchProvider",
    "DemoJobSearchProvider",
    "capability",
    "secret",
    "api_key",
    "provider requires",
    "is missing",
    "crank_joblisting",
    "crank_jobsourcecatalog",
    "crank_agentrun",
    "JobListing",
    "JobSourceCatalog",
    "AgentRun",
    "Traceback",
    "ADMIN",
)


class AssistantStatusTestCase(TestCase):
    """Base harness: clean cache, authenticated client, seedable inventory."""

    maxDiff = None

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("carol", "carol@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()

    # -- helpers ---------------------------------------------------------
    def _get(self):
        return self.client.get(reverse("agent-assistant-status"))

    def _body(self, response):
        return json.loads(response.content.decode("utf-8"))

    def _seed_inventory(self, *, enabled=True, approval="approved"):
        source = JobSourceCatalog.objects.create(
            name="Test Source",
            adapter_key="test-adapter",
            # Host must be on the code-owned SSRF allowlist (crank/agents/jobs/base.py).
            base_url="https://data.usajobs.gov/api/search",
            approval_state=getattr(JobSourceCatalog.ApprovalState, approval.upper()),
            enabled=enabled,
        )
        now = timezone.now()
        JobListing.objects.create(
            source=source,
            canonical_url="https://data.usajobs.gov/listings/1",
            employer_name="Example Corp",
            title="Backend Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
        )
        return source

    def _seed_pipeline_run(self, status) -> AgentRun:
        return AgentRun.objects.create(
            run_type=AgentRun.RunType.JOB_PIPELINE, status=status
        )


@override_settings(CACHES=LOCMEM, **READY_SETTINGS)
class StateClassificationTests(AssistantStatusTestCase):
    """Each state is produced by its defining condition (AC-2)."""

    def test_ready_when_enabled_with_inventory(self):
        self._seed_inventory()
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = self._body(response)
        self.assertEqual(body["state"], "ready")
        self.assertEqual(body["actions"], [])
        self.assertIn("checked_at", body)

    def test_replies_disabled_when_flag_off(self):
        # The feature flag gates a non-demo (orchestrator) configuration; with
        # the flag off and the orchestrator selected the assistant is
        # administratively disabled regardless of the environment.
        with override_settings(INTERACTIVE_AGENT_ENABLED=False, JOB_SEARCH_PROVIDER="orchestrator"):
            self.assertEqual(self._body(self._get())["state"], "replies_disabled")

    def test_replies_disabled_for_demo_provider_in_non_dev(self):
        with override_settings(JOB_SEARCH_PROVIDER="demo", ENV="prod"):
            self.assertEqual(self._body(self._get())["state"], "replies_disabled")

    def test_replies_disabled_for_unset_provider_in_non_dev(self):
        with override_settings(JOB_SEARCH_PROVIDER="", ENV="prod"):
            self.assertEqual(self._body(self._get())["state"], "replies_disabled")

    def test_demo_provider_with_flag_off_in_dev_is_ready_with_inventory(self):
        # The demo provider is the dev/test double and serves in dev regardless
        # of INTERACTIVE_AGENT_ENABLED (its send path never consults the flag),
        # so a dev demo configuration with a populated inventory is ready.
        self._seed_inventory()
        with override_settings(INTERACTIVE_AGENT_ENABLED=False):
            body = self._body(self._get())
        self.assertEqual(body["state"], "ready")
        self.assertEqual(body["actions"], [])

    def test_temporarily_unavailable_when_provider_construction_fails(self):
        # Orchestrator selected but the LLM configuration is broken at runtime.
        with override_settings(JOB_SEARCH_PROVIDER="orchestrator"):
            with patch(
                "crank.views.assistant_status._build_provider",
                side_effect=AssistantUnavailable(
                    "LLM_API_KEY is missing; provider requires a secret key"
                ),
            ):
                body = self._body(self._get())
        self.assertEqual(body["state"], "temporarily_unavailable")
        self.assertEqual(body["actions"], ["retry"])

    def test_temporarily_unavailable_on_unexpected_construction_error(self):
        # Defensive branch: an unexpected failure is still an outage to the
        # user, and its exception text is logged server-side, never serialized.
        with override_settings(JOB_SEARCH_PROVIDER="orchestrator"):
            with patch(
                "crank.views.assistant_status._build_provider",
                side_effect=ValueError("LLM_API_KEY leaked into a fake error"),
            ):
                body = self._body(self._get())
        self.assertEqual(body["state"], "temporarily_unavailable")

    def test_refreshing_while_pipeline_run_pending_or_running(self):
        self._seed_inventory()
        for status in (AgentRun.Status.PENDING, AgentRun.Status.RUNNING):
            cache.clear()
            AgentRun.objects.all().delete()
            run = self._seed_pipeline_run(status)
            self.assertEqual(self._body(self._get())["state"], "refreshing")
            run.status = AgentRun.Status.SUCCEEDED
            run.save(update_fields=["status"])
            cache.clear()

    def test_terminal_pipeline_run_does_not_block_ready(self):
        self._seed_inventory()
        self._seed_pipeline_run(AgentRun.Status.SUCCEEDED)
        self.assertEqual(self._body(self._get())["state"], "ready")

    def test_other_run_types_do_not_refresh(self):
        self._seed_inventory()
        self._seed_pipeline_run(AgentRun.Status.RUNNING)
        AgentRun.objects.all().update(run_type=AgentRun.RunType.CRAWL)
        cache.clear()
        self.assertEqual(self._body(self._get())["state"], "ready")

    def test_inventory_unavailable_without_any_sources(self):
        # No approved+enabled source at all: zero qualifying listings.
        self.assertEqual(self._body(self._get())["state"], "inventory_unavailable")
        body = self._body(self._get())
        self.assertEqual(body["actions"], ["browse_rankings"])

    def test_inventory_unavailable_when_source_disabled(self):
        self._seed_inventory(enabled=False)
        self.assertEqual(self._body(self._get())["state"], "inventory_unavailable")

    def test_inventory_unavailable_when_source_unapproved(self):
        self._seed_inventory(approval="pending")
        self.assertEqual(self._body(self._get())["state"], "inventory_unavailable")

    def test_inventory_unavailable_when_only_closed_listings(self):
        source = self._seed_inventory()
        JobListing.objects.all().update(status=JobListing.Status.CLOSED)
        cache.clear()
        del source
        self.assertEqual(self._body(self._get())["state"], "inventory_unavailable")


@override_settings(CACHES=LOCMEM)
class PrecedenceTests(AssistantStatusTestCase):
    """Higher-precedence states mask lower ones (AC-2 ordering)."""

    def test_signed_out_beats_everything(self):
        # Anonymous + replies-disabled configuration still reports signed_out.
        with override_settings(INTERACTIVE_AGENT_ENABLED=False, ENV="prod"):
            self.client.logout()
            self.assertEqual(
                json.loads(self.client.get(reverse("agent-assistant-status")).content)["state"],
                "signed_out",
            )

    def test_temporarily_unavailable_beats_refreshing_and_inventory(self):
        self.client.force_login(
            User.objects.create_user("dave", "dave@example.com", "pw")
        )
        with override_settings(
            INTERACTIVE_AGENT_ENABLED=True, JOB_SEARCH_PROVIDER="orchestrator", ENV="dev"
        ):
            with patch(
                "crank.views.assistant_status._build_provider",
                side_effect=AssistantUnavailable("LLM_API_KEY is missing"),
            ):
                self.assertEqual(
                    json.loads(self.client.get(reverse("agent-assistant-status")).content)["state"],
                    "temporarily_unavailable",
                )

    def test_refreshing_beats_inventory_unavailable(self):
        # Active pipeline run + empty inventory: the run wins (AC-2 example).
        self._seed_pipeline_run(AgentRun.Status.RUNNING)
        with override_settings(**READY_SETTINGS):
            self.assertEqual(self._body(self._get())["state"], "refreshing")

    def test_signed_out_checked_first_then_auth_state_classifies(self):
        with override_settings(**READY_SETTINGS):
            self._seed_inventory()
            self.client.logout()
            self.assertEqual(
                json.loads(self.client.get(reverse("agent-assistant-status")).content)["state"],
                "signed_out",
            )
            self.client.force_login(self.user)
            self.assertEqual(
                json.loads(self.client.get(reverse("agent-assistant-status")).content)["state"],
                "ready",
            )


@override_settings(CACHES=LOCMEM, **READY_SETTINGS)
class EnvelopeSafetyTests(AssistantStatusTestCase):
    """Bounded envelope: no internal strings ever serialized (AC-1/AC-5)."""

    def test_no_internal_strings_for_temporarily_unavailable(self):
        juicy = "LLM_API_KEY is missing; provider requires a secret key"
        with override_settings(JOB_SEARCH_PROVIDER="orchestrator"):
            with patch(
                "crank.views.assistant_status._build_provider",
                side_effect=AssistantUnavailable(juicy),
            ):
                content = self._get().content.decode("utf-8")
        for needle in FORBIDDEN_STRINGS:
            self.assertNotIn(needle, content)

    def test_no_internal_strings_for_replies_disabled(self):
        with override_settings(ENV="prod", JOB_SEARCH_PROVIDER="demo"):
            content = self.client.get(reverse("agent-assistant-status")).content.decode("utf-8")
        for needle in FORBIDDEN_STRINGS:
            self.assertNotIn(needle, content)

    def test_no_internal_strings_for_inventory_unavailable(self):
        content = self.client.get(reverse("agent-assistant-status")).content.decode("utf-8")
        for needle in FORBIDDEN_STRINGS:
            self.assertNotIn(needle, content)

    def test_staff_receive_the_same_public_envelope(self):
        self._seed_inventory()
        User.objects.create_superuser("root", "root@example.com", "pw")
        staff_client = Client()
        staff_client.force_login(User.objects.get(username="root"))
        staff_body = json.loads(staff_client.get(reverse("agent-assistant-status")).content)
        user_body = self._body(self._get())
        self.assertEqual(sorted(staff_body), sorted(user_body))
        self.assertEqual(staff_body, user_body)

    def test_envelope_keys_are_bounded(self):
        self._seed_inventory()
        body = self._body(self._get())
        self.assertEqual(sorted(body), ["actions", "checked_at", "state"])
        self.assertEqual(body["state"], "ready")
        self.assertEqual(body["actions"], [])
        self.assertIsInstance(body["checked_at"], str)


@override_settings(CACHES=LOCMEM, **READY_SETTINGS)
class CacheBehaviourTests(AssistantStatusTestCase):
    """TTL, clamping, and auth-state keying (AC-3)."""

    def test_second_request_within_ttl_hits_cache(self):
        # Inventory exists -> ready; then remove it. Within TTL the cached
        # ready state is served (the state is advisory and may trail reality).
        self._seed_inventory()
        self.assertEqual(self._body(self._get())["state"], "ready")
        JobListing.objects.all().delete()
        self.assertEqual(self._body(self._get())["state"], "ready")

    def test_state_change_after_cache_clear_is_reflected(self):
        JobListing.objects.all().delete()
        self.assertEqual(self._body(self._get())["state"], "inventory_unavailable")
        self._seed_inventory()
        cache.clear()
        self.assertEqual(self._body(self._get())["state"], "ready")

    def test_cache_keyed_on_auth_state(self):
        # The anonymous classification must never leak into the authenticated
        # cache entry (or vice versa).
        self.client.logout()
        self.assertEqual(
            json.loads(self.client.get(reverse("agent-assistant-status")).content)["state"],
            "signed_out",
        )
        self.client.force_login(self.user)
        self.assertEqual(self._body(self._get())["state"], "inventory_unavailable")

    def test_ttl_clamped_to_max(self):
        with override_settings(ASSISTANT_STATUS_CACHE_SECONDS=999):
            from crank.views import assistant_status as module

            self.assertEqual(module.cache_seconds(), module.MAX_CACHE_SECONDS)
            self.assertLessEqual(module.cache_seconds(), 60)

    def test_ttl_zero_disables_caching(self):
        with override_settings(ASSISTANT_STATUS_CACHE_SECONDS=0):
            JobListing.objects.all().delete()
            self.assertEqual(self._body(self._get())["state"], "inventory_unavailable")
            self._seed_inventory()
            # No cache entry was written: the very next request reclassifies.
            self.assertEqual(self._body(self._get())["state"], "ready")

    def test_invalid_ttl_falls_back_to_default(self):
        from crank.views import assistant_status as module

        with override_settings(ASSISTANT_STATUS_CACHE_SECONDS="not-a-number"):
            self.assertEqual(module.cache_seconds(), module.DEFAULT_CACHE_SECONDS)
        with override_settings(ASSISTANT_STATUS_CACHE_SECONDS=-5):
            self.assertEqual(module.cache_seconds(), 0)

    def test_default_ttl_within_bound(self):
        from django.conf import settings as dj_settings

        from crank.views import assistant_status as module

        configured = getattr(
            dj_settings, "ASSISTANT_STATUS_CACHE_SECONDS", module.DEFAULT_CACHE_SECONDS
        )
        self.assertLessEqual(int(configured), module.MAX_CACHE_SECONDS)


@override_settings(CACHES=LOCMEM, **READY_SETTINGS)
class HttpSemanticsTests(AssistantStatusTestCase):
    """Method contract, audience parity, and advisory-only behavior (AC-3/AC-8)."""

    def test_post_is_not_allowed(self):
        response = self.client.post(reverse("agent-assistant-status"))
        self.assertEqual(response.status_code, 405)

    def test_put_is_not_allowed(self):
        response = self.client.put(
            reverse("agent-assistant-status"), data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 405)

    def test_anonymous_request_is_200_json_without_login_redirect(self):
        self.client.logout()
        response = self.client.get(reverse("agent-assistant-status"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        body = json.loads(response.content.decode("utf-8"))
        self.assertEqual(body["state"], "signed_out")

    def test_status_check_performs_no_conversation_or_preference_writes(self):
        self._seed_inventory()
        self._get()
        self.assertFalse(JobSearchConversation.objects.exists())
        self.assertFalse(JobSearchConversation.objects.filter(owner=self.user).exists())
        # No message rows either: the status path is strictly read-only.
        from crank.models import JobSearchMessage

        self.assertFalse(JobSearchMessage.objects.exists())

    def test_checked_at_is_iso8601(self):
        body = self._body(self._get())
        parsed = datetime.fromisoformat(body["checked_at"])
        self.assertLessEqual((timezone.now() - parsed).total_seconds(), 60)
