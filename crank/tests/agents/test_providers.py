# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the LLM-backed ProviderGateway and OrchestratorJobSearchProvider.

These tests verify:
- LLMGateway translates ModelRequest→LLMRequest and LLMResult→GatewayResponse
- LLMGateway maps LLM errors to typed job-search errors
- OrchestratorJobSearchProvider returns grounded replies (org names, not echoes)
- OrchestratorJobSearchProvider maps preference patches to the transport contract
- Missing API key surfaces as a friendly error, not a 500
- No live network calls (all via fake transports/gateways)
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings

from crank.agents.job_search.demo import JobSearchServiceError
from crank.agents.job_search.errors import (
    CostLimitError,
    InvalidOrganizationReferenceError,
    ProviderError,
    ProviderTimeoutError,
)
from crank.agents.job_search.gateway import GatewayResponse, ModelRequest
from crank.agents.job_search.providers import (
    _NO_OWNER,
    _RESPONSE_SCHEMA,
    LLMGateway,
    OrchestratorJobSearchProvider,
    _NullPreferenceService,
    _PreferenceServiceAdapter,
)
from crank.agents.job_search.service import JobSearchOrchestrator
from crank.models import JobSearchConversation, JobSearchMessage


def _llm():
    """Return the current crank.agents.llm module (survives reloads)."""
    import crank.agents.llm as _m
    return _m


# ---------------------------------------------------------------------------
# Fake LLM provider for testing LLMGateway
# ---------------------------------------------------------------------------


class FakeLLM:
    """Controllable offline LLM provider for testing LLMGateway.

    Does not inherit from BaseLLMProvider so it avoids the module-reload identity
    issue. Implements the LLMProvider protocol structurally.
    """

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        mod = _llm()
        content = self._result or '{"message": "ok", "cited_organization_ids": [], "cited_job_listing_ids": [], "preference_patch": null}'
        return mod.LLMResult(
            content=content,
            data=None,
            provider="fake-llm-gw",
            model="fake",
            usage=mod.LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=1,
            correlation_id=request.correlation_id,
        )


def make_fake_llm(result=None, exc=None):
    # If exc is a class, instantiate it from the current module to avoid
    # identity issues caused by test_llm.py reloading crank.agents.llm.
    if exc is not None and isinstance(exc, type):
        exc = exc("test")
    return FakeLLM(result=result, exc=exc)


# ---------------------------------------------------------------------------
# LLMGateway tests
# ---------------------------------------------------------------------------


class LLMGatewayTests(SimpleTestCase):
    def test_complete_translates_request_and_response(self):
        llm = make_fake_llm(result='{"message": "Hello!", "cited_organization_ids": [1], "cited_job_listing_ids": [], "preference_patch": null}')
        gw = LLMGateway(provider=llm)
        response = gw.complete(
            ModelRequest(
                prompt_id="test-prompt",
                system="You are a helpful assistant.",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        self.assertIsInstance(response, GatewayResponse)
        self.assertIn("Hello!", response.text)
        self.assertEqual(response.usage["prompt_tokens"], 10)
        self.assertEqual(response.usage["output_tokens"], 5)

    def test_system_messages_merged(self):
        """Multiple system messages are merged into one to avoid duplicate roles."""
        llm = make_fake_llm()
        gw = LLMGateway(provider=llm)
        gw.complete(
            ModelRequest(
                prompt_id="test",
                system="Base system.",
                messages=[
                    {"role": "system", "content": "Additional context."},
                    {"role": "user", "content": "hi"},
                ],
            )
        )
        self.assertEqual(llm.calls, 1)

    def test_timeout_error_mapped(self):
        mod = _llm()
        llm = make_fake_llm(exc=mod.LLMTimeoutError("timeout"))
        gw = LLMGateway(provider=llm)
        with pytest.raises(ProviderTimeoutError):
            gw.complete(ModelRequest(prompt_id="t", system="", messages=[]))

    def test_usage_limit_error_mapped(self):
        mod = _llm()
        llm = make_fake_llm(exc=mod.LLMUsageLimitError("budget exceeded"))
        gw = LLMGateway(provider=llm)
        with pytest.raises(CostLimitError):
            gw.complete(ModelRequest(prompt_id="t", system="", messages=[]))

    def test_provider_error_mapped(self):
        mod = _llm()
        llm = make_fake_llm(exc=mod.LLMProviderError("provider failed"))
        gw = LLMGateway(provider=llm)
        with pytest.raises(ProviderError):
            gw.complete(ModelRequest(prompt_id="t", system="", messages=[]))

    def test_config_error_mapped_to_provider_error(self):
        mod = _llm()
        llm = make_fake_llm(exc=mod.LLMConfigurationError("not configured"))
        gw = LLMGateway(provider=llm)
        with pytest.raises(ProviderError):
            gw.complete(ModelRequest(prompt_id="t", system="", messages=[]))

    def test_generic_llm_error_mapped_to_provider_error(self):
        """The base LLMError (not a subclass) maps to ProviderError."""
        mod = _llm()
        llm = make_fake_llm(exc=mod.LLMError("generic"))
        gw = LLMGateway(provider=llm)
        with pytest.raises(ProviderError):
            gw.complete(ModelRequest(prompt_id="t", system="", messages=[]))

    def test_response_schema_included_in_llm_request(self):
        llm = make_fake_llm()
        gw = LLMGateway(provider=llm)
        gw.complete(
            ModelRequest(
                prompt_id="schema-test",
                system="sys",
                messages=[{"role": "user", "content": "q"}],
            )
        )
        self.assertEqual(llm.calls, 1)


# ---------------------------------------------------------------------------
# OrchestratorJobSearchProvider tests (using fake gateway)
# ---------------------------------------------------------------------------


ORG_ACME = SimpleNamespace(id=1, name="Acme Inc", url="https://acme.example",
                           funding_round="A", rto_policy="R")
ORG_GLOBEX = SimpleNamespace(id=2, name="Globex", url="https://globex.example",
                             funding_round="S", rto_policy="H")


class FakeGateway:
    """Fake ProviderGateway for orchestrator tests."""

    def __init__(self, result=None, exc=None):
        self.result = result if result is not None else {}
        self.exc = exc
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return GatewayResponse(text=json.dumps(self.result), usage={"output_tokens": 12})


class FakePreferenceService:
    def __init__(self, apply_result=False):
        self.apply_result = apply_result
        self.validate_calls = 0
        self.apply_calls = 0

    def validate_patch(self, patch):
        self.validate_calls += 1

    def apply_patch(self, patch):
        self.apply_calls += 1
        return self.apply_result


class _OwnerScopedPref:
    """Owner-bound preference service double (mirrors ``_PreferenceServiceAdapter``)."""

    def __init__(self, user):
        self._user = user

    def validate_patch(self, patch):
        pass

    def apply_patch(self, patch):
        return False


class _NullPreferenceServiceTests(SimpleTestCase):
    def test_null_preference_service_validate_passes(self):
        svc = _NullPreferenceService()
        svc.validate_patch({"any": "thing"})

    def test_null_preference_service_apply_returns_false(self):
        svc = _NullPreferenceService()
        self.assertFalse(svc.apply_patch({"any": "thing"}))


class OrchestratorProviderTests(SimpleTestCase):
    """Unit tests for OrchestratorJobSearchProvider with fake gateway."""

    def _make_provider(self, gateway_result=None, gateway_exc=None, pref=None):
        gw = FakeGateway(result=gateway_result, exc=gateway_exc)
        preference = pref or FakePreferenceService()
        orchestrator = JobSearchOrchestrator(
            gateway=gw,
            preference_service=preference,
            org_datasource=lambda filters, limit: [ORG_ACME, ORG_GLOBEX],
            score_datasource=lambda ids, types, limit: [],
            job_listing_datasource=lambda filters, limit: [],
        )
        return OrchestratorJobSearchProvider(orchestrator=orchestrator)

    def test_grounded_reply_includes_org_name_not_echo(self):
        """With a real orchestrator, replies reference org names from the catalog."""
        provider = self._make_provider(
            gateway_result={
                "message": "I recommend Globex for remote seed-stage work.",
                "cited_organization_ids": [2],
                "cited_job_listing_ids": [],
                "preference_patch": None,
            },
        )
        conv = SimpleNamespace(
            pk=1,
            owner_id=1,
            messages=SimpleNamespace(
                order_by=lambda *a, **kw: [
                    SimpleNamespace(role="user", content="I want remote work"),
                    SimpleNamespace(role="assistant", content="Noted."),
                ]
            ),
        )
        with patch.object(
            OrchestratorJobSearchProvider,
            "_get_preference_markdown",
            return_value="",
        ):
            reply, changed, _ = provider.generate_reply(
                conversation=conv, user_message="recommend seed startups"
            )
        self.assertIn("Globex", reply)
        self.assertFalse(changed)

    def test_preference_patch_maps_to_changed_flag(self):
        provider = self._make_provider(
            gateway_result={
                "message": "Updated your preferences.",
                "cited_organization_ids": [],
                "cited_job_listing_ids": [],
                "preference_patch": {"set": {"funding_stage": ["S"]}},
            },
            pref=FakePreferenceService(apply_result=True),
        )
        conv = SimpleNamespace(
            pk=1,
            owner_id=1,
            messages=SimpleNamespace(order_by=lambda *a, **kw: []),
        )
        with patch.object(
            OrchestratorJobSearchProvider,
            "_get_preference_markdown",
            return_value="",
        ):
            reply, changed, _ = provider.generate_reply(
                conversation=conv, user_message="prefer seed"
            )
        self.assertTrue(changed)
        self.assertIn("Updated", reply)

    def test_provider_error_propagates(self):
        provider = self._make_provider(gateway_exc=ProviderError("down"))
        conv = SimpleNamespace(
            pk=1,
            owner_id=1,
            messages=SimpleNamespace(order_by=lambda *a, **kw: []),
        )
        with patch.object(
            OrchestratorJobSearchProvider,
            "_get_preference_markdown",
            return_value="",
        ), pytest.raises(ProviderError):
            provider.generate_reply(conversation=conv, user_message="hi")

    def test_timeout_error_propagates(self):
        provider = self._make_provider(gateway_exc=ProviderTimeoutError("slow"))
        conv = SimpleNamespace(
            pk=1,
            owner_id=1,
            messages=SimpleNamespace(order_by=lambda *a, **kw: []),
        )
        with patch.object(
            OrchestratorJobSearchProvider,
            "_get_preference_markdown",
            return_value="",
        ), pytest.raises(ProviderTimeoutError):
            provider.generate_reply(conversation=conv, user_message="hi")

    def test_hallucinated_org_id_rejected(self):
        provider = self._make_provider(
            gateway_result={
                "message": "Check out org 999.",
                "cited_organization_ids": [999],
                "cited_job_listing_ids": [],
                "preference_patch": None,
            },
        )
        conv = SimpleNamespace(
            pk=1,
            owner_id=1,
            messages=SimpleNamespace(order_by=lambda *a, **kw: []),
        )
        with patch.object(
            OrchestratorJobSearchProvider,
            "_get_preference_markdown",
            return_value="",
        ), pytest.raises(InvalidOrganizationReferenceError):
            provider.generate_reply(conversation=conv, user_message="hi")

    def test_cross_owner_reuse_does_not_leak_orchestrator(self):
        """A provider reused across two DIFFERENT owners never leaks the first
        owner's orchestrator: the second owner gets an orchestrator built for
        the second owner, never the first (regression for #432).

        The orchestrator is lazy-built and cached *per owner*; when the same
        provider sees a new owner it must rebuild rather than return the first
        owner's cached orchestrator.
        """
        alice = SimpleNamespace(pk=1, username="alice")
        bob = SimpleNamespace(pk=2, username="bob")

        # No shared match service is injected, so each owner's orchestrator gets
        # its own owner-scoped match wiring via ``_matches_for_user``.
        provider = OrchestratorJobSearchProvider(
            gateway=FakeGateway(),
            preference_service=FakePreferenceService(),
        )

        orch_alice = provider._ensure_orchestrator(alice)
        orch_bob = provider._ensure_orchestrator(bob)

        # Distinct owners must never share an orchestrator — bob gets his own
        # orchestrator wired to bob, not alice's cached one.
        self.assertIsNot(orch_alice, orch_bob)
        self.assertIs(orch_alice._user, alice)
        self.assertIs(orch_bob._user, bob)
        self.assertIsNot(orch_bob._user, alice)
        # Each owner gets its own match wiring, never the first owner's.
        self.assertIsNot(orch_bob._match_service, orch_alice._match_service)

        # Same owner still reuses its cached orchestrator (lazy-build retained).
        self.assertIs(provider._ensure_orchestrator(alice), orch_alice)
        self.assertIs(provider._ensure_orchestrator(bob), orch_bob)

    # -- MAJOR-1: no supported injection path may leak owner A's wiring to B ---

    def test_injected_preference_service_bound_different_owner_fails_closed(self):
        """MAJOR-1 path (b): an owner-bound preference service (the production
        ``_PreferenceServiceAdapter``) can only ever serve its own owner.

        Bob's request must raise rather than be wired to Alice's adapter — a
        preference patch from Bob would otherwise be persisted to Alice's row.
        """
        alice = SimpleNamespace(pk=1, username="alice")
        bob = SimpleNamespace(pk=2, username="bob")
        provider = OrchestratorJobSearchProvider(
            gateway=FakeGateway(),
            preference_service=_PreferenceServiceAdapter(alice),
        )

        # The injected adapter is wired only for its own owner.
        self.assertIs(
            provider._ensure_orchestrator(alice)._preference_service,
            provider._preference_service,
        )
        # A different owner must never receive (or be wired to) Bob's adapter.
        with pytest.raises(ValueError):
            provider._ensure_orchestrator(bob)

    def test_injected_orchestrator_bound_other_owner_fails_closed(self):
        """MAJOR-1 path (a): an injected, owner-bound orchestrator (``_user``
        set) can only ever serve that owner. A different owner's request fails
        closed instead of receiving the first owner's orchestrator."""
        alice = SimpleNamespace(pk=1, username="alice")
        bob = SimpleNamespace(pk=2, username="bob")
        orch_alice = JobSearchOrchestrator(
            gateway=FakeGateway(),
            preference_service=FakePreferenceService(),
            user=alice,
        )
        provider = OrchestratorJobSearchProvider(orchestrator=orch_alice)

        self.assertIs(provider._ensure_orchestrator(alice), orch_alice)
        with pytest.raises(ValueError):
            provider._ensure_orchestrator(bob)

    def test_bound_injection_reused_for_same_owner_across_instances(self):
        """An owner-bound artifact is only rejected for a DIFFERENT owner: the
        same database user materialized as a new instance is still allowed."""
        alice = SimpleNamespace(pk=1, username="alice")
        alice_again = SimpleNamespace(pk=1, username="alice")
        provider = OrchestratorJobSearchProvider(
            gateway=FakeGateway(),
            preference_service=_PreferenceServiceAdapter(alice),
        )
        self.assertIs(
            provider._ensure_orchestrator(alice_again)._preference_service,
            provider._preference_service,
        )

    def test_preference_service_factory_wires_per_owner(self):
        """MAJOR-1 remedy: per-owner preference factory builds fresh,
        owner-bound wiring for each owner — never the first owner's adapter."""
        alice = SimpleNamespace(pk=1, username="alice")
        bob = SimpleNamespace(pk=2, username="bob")

        def factory(user):
            return _OwnerScopedPref(user)

        provider = OrchestratorJobSearchProvider(
            gateway=FakeGateway(),
            preference_service_factory=factory,
        )
        orch_alice = provider._ensure_orchestrator(alice)
        orch_bob = provider._ensure_orchestrator(bob)

        self.assertIsNot(orch_alice, orch_bob)
        self.assertIs(orch_alice._preference_service._user, alice)
        self.assertIs(orch_bob._preference_service._user, bob)
        self.assertIsNot(orch_alice._preference_service, orch_bob._preference_service)

    def test_orchestrator_factory_wires_per_owner(self):
        """MAJOR-1 safe path: an injected orchestrator factory yields a fresh,
        owner-bound orchestrator per owner instead of one fixed orchestrator."""
        alice = SimpleNamespace(pk=1, username="alice")
        bob = SimpleNamespace(pk=2, username="bob")

        def factory(user):
            return JobSearchOrchestrator(
                gateway=FakeGateway(),
                preference_service=_OwnerScopedPref(user),
                user=user,
            )

        provider = OrchestratorJobSearchProvider(orchestrator_factory=factory)
        orch_alice = provider._ensure_orchestrator(alice)
        orch_bob = provider._ensure_orchestrator(bob)

        self.assertIsNot(orch_alice, orch_bob)
        self.assertIs(orch_alice._user, alice)
        self.assertIs(orch_bob._user, bob)

    # -- MINOR-1: bounded cache --------------------------------------------

    def test_orchestrator_cache_is_bounded_lru(self):
        """The per-owner cache is bounded: evicting the least-recently-used
        owner releases its orchestrator, so a shared, long-lived provider never
        retains every owner seen (MINOR-1)."""
        provider = OrchestratorJobSearchProvider(gateway=FakeGateway())
        provider._cache_size = 2
        alice = SimpleNamespace(pk=1, username="alice")
        bob = SimpleNamespace(pk=2, username="bob")
        carol = SimpleNamespace(pk=3, username="carol")

        orch_alice = provider._ensure_orchestrator(alice)
        orch_bob = provider._ensure_orchestrator(bob)
        self.assertIs(provider._ensure_orchestrator(alice), orch_alice)  # touch alice
        orch_carol = provider._ensure_orchestrator(carol)  # capacity 2 -> evicts bob

        self.assertEqual(len(provider._orchestrators), 2)
        self.assertIs(provider._ensure_orchestrator(alice), orch_alice)  # still cached
        self.assertIsNot(provider._ensure_orchestrator(bob), orch_bob)  # rebuilt after eviction

    # -- MINOR-2: stable owner identity ------------------------------------

    def test_cache_keyed_by_stable_owner_identity(self):
        """Two instances materializing the same DB row (same label+pk) share
        one cached orchestrator rather than building duplicates (MINOR-2)."""
        alice = SimpleNamespace(pk=7, username="alice")
        alice_again = SimpleNamespace(pk=7, username="alice")
        provider = OrchestratorJobSearchProvider(gateway=FakeGateway())

        orch_a = provider._ensure_orchestrator(alice)
        orch_a2 = provider._ensure_orchestrator(alice_again)

        self.assertIs(orch_a, orch_a2)
        self.assertEqual(provider._owner_key(alice), ("SimpleNamespace", 7))

    def test_unsaved_user_not_shared_across_instances(self):
        """Unsaved owners (no pk) have no stable identity and are never shared
        across distinct instances (explicit unsaved-user behavior, MINOR-2)."""
        provider = OrchestratorJobSearchProvider(gateway=FakeGateway())
        a = SimpleNamespace(pk=None, username="who")
        b = SimpleNamespace(pk=None, username="who")

        orch_a = provider._ensure_orchestrator(a)
        orch_b = provider._ensure_orchestrator(b)
        self.assertIsNot(orch_a, orch_b)
        self.assertIs(provider._ensure_orchestrator(a), orch_a)

    # -- NIT-1 / NIT-2: sentinel key & no-owner contract -------------------

    def test_no_owner_sentinel_reused_and_distinct(self):
        """NIT-1/NIT-2: ``_NO_OWNER`` is a valid cache key (typing excludes no
        real key) and repeated ``None`` calls reuse the no-owner entry, distinct
        from every real-owner entry."""
        self.assertIs(OrchestratorJobSearchProvider._owner_key(None), _NO_OWNER)
        alice = SimpleNamespace(pk=1, username="alice")
        provider = OrchestratorJobSearchProvider(gateway=FakeGateway())

        orch_none1 = provider._ensure_orchestrator(None)
        orch_none2 = provider._ensure_orchestrator(None)
        self.assertIs(orch_none1, orch_none2)  # repeated None reuses the entry
        self.assertIsInstance(orch_none1._preference_service, _NullPreferenceService)

        orch_alice = provider._ensure_orchestrator(alice)
        self.assertIsNot(orch_none1, orch_alice)  # distinct from real owner
        self.assertIs(provider._ensure_orchestrator(None), orch_none1)  # retained


class OrchestratorProviderErrorMappingTests(SimpleTestCase):
    """Cover all error-mapping branches in generate_reply."""

    def _make_provider_with_mock_orchestrator(self, side_effect):
        """Build a provider whose orchestrator.run() raises the given exception."""
        orchestrator = MagicMock()
        # The mock stands in for an owner-neutral injected orchestrator (the
        # error-mapping tests don't exercise owner identity), so it must not be
        # mistaken for an owner-bound orchestrator and rejected.
        orchestrator._user = None
        orchestrator.run.side_effect = side_effect
        provider = OrchestratorJobSearchProvider(orchestrator=orchestrator)
        # Skip preference markdown DB lookup
        provider._get_preference_markdown = lambda conv: ""
        return provider

    def test_provider_error_mapped_by_generate_reply(self):
        provider = self._make_provider_with_mock_orchestrator(
            ProviderError("down")
        )
        conv = SimpleNamespace(pk=1, owner_id=1, messages=SimpleNamespace(
            order_by=lambda *a, **kw: []))
        with pytest.raises(ProviderError):
            provider.generate_reply(conversation=conv, user_message="hi")

    def test_timeout_error_mapped_by_generate_reply(self):
        provider = self._make_provider_with_mock_orchestrator(
            ProviderTimeoutError("timeout")
        )
        conv = SimpleNamespace(pk=1, owner_id=1, messages=SimpleNamespace(
            order_by=lambda *a, **kw: []))
        with pytest.raises(ProviderTimeoutError):
            provider.generate_reply(conversation=conv, user_message="hi")

    def test_cost_limit_error_mapped_by_generate_reply(self):
        provider = self._make_provider_with_mock_orchestrator(
            CostLimitError("limit")
        )
        conv = SimpleNamespace(pk=1, owner_id=1, messages=SimpleNamespace(
            order_by=lambda *a, **kw: []))
        with pytest.raises(CostLimitError):
            provider.generate_reply(conversation=conv, user_message="hi")

    def test_job_search_error_mapped_by_generate_reply(self):
        from crank.agents.job_search.errors import JobSearchError
        provider = self._make_provider_with_mock_orchestrator(
            JobSearchError("generic job search error")
        )
        conv = SimpleNamespace(pk=1, owner_id=1, messages=SimpleNamespace(
            order_by=lambda *a, **kw: []))
        with pytest.raises(JobSearchError):
            provider.generate_reply(conversation=conv, user_message="hi")

    def test_llm_config_error_surfaces_as_job_search_service_error(self):
        from crank.agents.llm import LLMConfigurationError
        provider = self._make_provider_with_mock_orchestrator(
            LLMConfigurationError("not configured")
        )
        conv = SimpleNamespace(pk=1, owner_id=1, messages=SimpleNamespace(
            order_by=lambda *a, **kw: []))
        with pytest.raises(JobSearchServiceError):
            provider.generate_reply(conversation=conv, user_message="hi")

    def test_generic_exception_not_config_error_reraises(self):
        provider = self._make_provider_with_mock_orchestrator(
            RuntimeError("something unexpected")
        )
        conv = SimpleNamespace(pk=1, owner_id=1, messages=SimpleNamespace(
            order_by=lambda *a, **kw: []))
        with pytest.raises(RuntimeError):
            provider.generate_reply(conversation=conv, user_message="hi")

    def test_history_build_failure_logs_and_reraises(self):
        """Exercises the except-block that catches history/preference failures."""
        orchestrator = MagicMock()
        orchestrator._user = None  # owner-neutral mock, so owner checks pass
        provider = OrchestratorJobSearchProvider(orchestrator=orchestrator)

        def _build_failing(conversation):
            raise ValueError("bad conversation")
        provider._build_conversation_history = _build_failing
        provider._get_preference_markdown = lambda conv: ""

        conv = SimpleNamespace(pk=1, owner_id=1, messages=SimpleNamespace(
            order_by=lambda *a, **kw: []))
        with pytest.raises(ValueError):
            provider.generate_reply(conversation=conv, user_message="hi")


class OrchestratorProviderIntegrationTests(TestCase):
    """Integration tests with Django models (conversations, messages)."""

    def setUp(self):
        self.user = User.objects.create_user("testuser", "test@example.com", "pw")

    def test_generate_reply_with_real_conversation(self):
        """End-to-end: conversation model → orchestrator provider → grounded reply."""
        conv = JobSearchConversation.objects.create(owner=self.user)
        JobSearchMessage.objects.create(
            conversation=conv, role="user", content="I want remote work"
        )
        JobSearchMessage.objects.create(
            conversation=conv, role="assistant", content="Noted your preference."
        )

        gw = FakeGateway(result={
            "message": "Based on your preferences, Globex is a strong match.",
            "cited_organization_ids": [2],
            "cited_job_listing_ids": [],
            "preference_patch": None,
        })
        orchestrator = JobSearchOrchestrator(
            gateway=gw,
            preference_service=FakePreferenceService(),
            org_datasource=lambda filters, limit: [ORG_ACME, ORG_GLOBEX],
            score_datasource=lambda ids, types, limit: [],
            job_listing_datasource=lambda filters, limit: [],
        )
        provider = OrchestratorJobSearchProvider(orchestrator=orchestrator)

        with patch.object(
            OrchestratorJobSearchProvider,
            "_get_preference_markdown",
            return_value="",
        ):
            reply, changed, results = provider.generate_reply(
                conversation=conv, user_message="what about seed startups?"
            )
        self.assertIn("Globex", reply)
        self.assertFalse(changed)

    def test_get_preference_markdown_fetches_real_preferences(self):
        """The real _get_preference_markdown reads UserPreference from DB."""
        from crank.models.preference import UserPreference
        conv = JobSearchConversation.objects.create(owner=self.user)
        # No preference → empty string
        self.assertEqual(
            OrchestratorJobSearchProvider._get_preference_markdown(conv),
            "",
        )
        # Create a preference → returns markdown
        UserPreference.objects.create(
            user=self.user,
            preferences_markdown="**Remote only**",
        )
        self.assertEqual(
            OrchestratorJobSearchProvider._get_preference_markdown(conv),
            "**Remote only**",
        )

    def test_get_preference_markdown_exception_returns_empty(self):
        """If the preference model is unavailable, the except returns empty string."""
        import sys
        conv = JobSearchConversation.objects.create(owner=self.user)
        # Remove preference module to force ImportError inside the try block
        with patch.dict(sys.modules, {"crank.models.preference": None}):
            self.assertEqual(
                OrchestratorJobSearchProvider._get_preference_markdown(conv),
                "",
            )


class JobSearchServiceOrchestratorTests(TestCase):
    """Test that JOB_SEARCH_PROVIDER=orchestrator is wired through JobSearchService."""

    def setUp(self):
        self.user = User.objects.create_user("svcuser", "svc@example.com", "pw")

    @override_settings(
        JOB_SEARCH_PROVIDER="orchestrator",
        LLM_PROVIDER="crank.agents.llm:FakeLLMProvider",
        LLM_MODEL="",
        INTERACTIVE_AGENT_ENABLED=True,
    )
    def test_orchestrator_selected_via_settings(self):
        """JOB_SEARCH_PROVIDER=orchestrator selects OrchestratorJobSearchProvider."""
        from crank.agents.job_search.demo import _build_provider
        provider = _build_provider()
        self.assertIsInstance(provider, OrchestratorJobSearchProvider)

    @override_settings(JOB_SEARCH_PROVIDER="demo")
    def test_demo_selected_via_default(self):
        from crank.agents.job_search.demo import _build_provider
        provider = _build_provider()
        self.assertEqual(type(provider).__name__, "DemoJobSearchProvider")

    @override_settings(JOB_SEARCH_PROVIDER="orchestrator")
    def test_orchestrator_missing_key_friendly_error(self):
        """Missing API key surfaces as JobSearchServiceError, not a 500."""
        from crank.agents.job_search.demo import _build_provider
        with self.assertRaises(JobSearchServiceError):
            _build_provider()

    @override_settings(JOB_SEARCH_PROVIDER="unknown")
    def test_unknown_provider_raises(self):
        from crank.agents.job_search.demo import _build_provider
        with self.assertRaises(JobSearchServiceError):
            _build_provider()

    @override_settings(JOB_SEARCH_PROVIDER="demo", ENV="prod")
    def test_demo_provider_is_disabled_in_non_dev(self):
        """The demo simulator must never serve production traffic (issue #423)."""
        from crank.agents.job_search.demo import _build_provider
        with self.assertRaises(JobSearchServiceError):
            _build_provider()

    @override_settings(JOB_SEARCH_PROVIDER="demo", ENV="staging")
    def test_demo_provider_is_disabled_in_staging(self):
        from crank.agents.job_search.demo import _build_provider
        with self.assertRaises(JobSearchServiceError):
            _build_provider()

    @override_settings(JOB_SEARCH_PROVIDER="demo", ENV="dev")
    def test_demo_provider_is_allowed_in_dev(self):
        from crank.agents.job_search.demo import _build_provider
        self.assertEqual(
            type(_build_provider()).__name__, "DemoJobSearchProvider"
        )

    def test_demo_echo_reply_is_rejected_by_service(self):
        """Anti-echo guard applies to the configured demo path (issue #423)."""
        from crank.agents.job_search.demo import (
            DemoJobSearchProvider,
            JobSearchService,
        )

        class EchoProvider(DemoJobSearchProvider):
            def generate_reply(self, *, conversation, user_message):
                return user_message, False, None  # verbatim echo

        conv = JobSearchConversation.objects.create(owner=self.user)
        svc = JobSearchService(provider=EchoProvider())
        with self.assertRaises(JobSearchServiceError):
            svc.run_turn(conversation=conv, user_message="show me jobs")

    def test_demo_grounded_reply_is_not_rejected(self):
        """The demo's non-echo canned replies still pass the guard."""
        from crank.agents.job_search.demo import (
            DemoJobSearchProvider,
            JobSearchService,
        )

        conv = JobSearchConversation.objects.create(owner=self.user)
        svc = JobSearchService(provider=DemoJobSearchProvider())
        reply, changed, _ = svc.run_turn(
            conversation=conv, user_message="show me jobs"
        )
        self.assertTrue(reply)


class ResponseSchemaTests(SimpleTestCase):
    """Verify the response schema matches AssistantCompletion contract."""

    def test_schema_has_required_keys(self):
        self.assertIn("message", _RESPONSE_SCHEMA["properties"])
        self.assertIn("cited_organization_ids", _RESPONSE_SCHEMA["properties"])
        self.assertIn("cited_job_listing_ids", _RESPONSE_SCHEMA["properties"])
        self.assertIn("preference_patch", _RESPONSE_SCHEMA["properties"])
        self.assertEqual(
            set(_RESPONSE_SCHEMA["required"]),
            {"message", "cited_organization_ids", "cited_job_listing_ids", "preference_patch"},
        )
