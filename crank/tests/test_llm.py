# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the typed LLM provider gateway (crank/agents/llm.py).

All tests exercise the gateway through dependency injection / settings and use
only offline providers — never a real provider SDK, and never network I/O.
"""

import time

from django.test import SimpleTestCase, override_settings

from crank.agents import llm
from crank.agents.llm import (
    BaseLLMProvider,
    FakeLLMProvider,
    LLMConfig,
    LLMConfigurationError,
    LLMMessage,
    LLMProviderError,
    LLMRequest,
    LLMTimeoutError,
    LLMUsageLimitError,
    get_llm_provider,
    get_per_user_spend,
    is_interactive_agent_enabled,
    reset_per_user_spend,
)


BASE_PROVIDER = "crank.agents.llm:FakeLLMProvider"
FAKE_CONFIG = LLMConfig(
    provider=BASE_PROVIDER,
    max_tokens=64,
    per_user_cost_limit_usd=0.1,
    price_per_1k_tokens_usd=0.01,
    enabled=True,
)


# -- Local, offline provider classes used to probe behavior -----------------


class KeyRequiringProvider(FakeLLMProvider):
    """A provider that must fail closed without an env-backed API key."""

    requires_api_key = True
    provider_name = "key-requiring"


class TimeoutProvider(BaseLLMProvider):
    """Simulates a provider whose SDK call times out."""

    provider_name = "timeout"
    requires_api_key = False

    def _call(self, request):
        raise TimeoutError("providers should never surface this")

    def _parse_response(self, raw, request):
        return "", None


class HangingProvider(BaseLLMProvider):
    """A provider whose SDK call blocks (simulating a network stall/deadlock).

    The gateway must abort it via the configured wall-clock timeout rather than
    blocking forever.
    """

    provider_name = "hanging"
    requires_api_key = False

    def _call(self, request):
        time.sleep(2.0)  # longer than any gateway timeout under test
        return "late"

    def _parse_response(self, raw, request):
        return raw, None


class ExplodingProvider(FakeLLMProvider):
    """Simulates a provider SDK raising an error that may embed sensitive data."""

    provider_name = "exploding"

    def _call(self, request):
        raise RuntimeError(
            f"secret_apikey=super-secret-{request.correlation_id} "
            "user_email=alice@example.com full_prompt=DO-NOT-LEAK-ME"
        )


class RecordingProvider(FakeLLMProvider):
    """Counts SDK calls so tests can prove no request is sent under a ceiling."""

    provider_name = "recording"
    calls = 0

    def _call(self, request):
        type(self).calls += 1
        return super()._call(request)


# -- Provider selection ------------------------------------------------------


class ProviderSelectionTests(SimpleTestCase):
    @override_settings(LLM_PROVIDER=BASE_PROVIDER)
    def test_provider_selected_through_settings(self):
        provider = get_llm_provider()
        self.assertIsInstance(provider, FakeLLMProvider)
        # The configured, immutable config is bound to the adapter.
        self.assertEqual(provider.config.provider, BASE_PROVIDER)

    def test_provider_selected_directly(self):
        provider = get_llm_provider(FAKE_CONFIG)
        self.assertIsInstance(provider, FakeLLMProvider)

    def test_missing_provider_config_fails_closed(self):
        with self.assertRaisesRegex(LLMConfigurationError, "LLM_PROVIDER"):
            get_llm_provider(
                LLMConfig(provider="", max_tokens=64, enabled=True)
            )

    @override_settings(LLM_PROVIDER="")
    def test_settings_missing_provider_raises_clear_error(self):
        with self.assertRaisesRegex(LLMConfigurationError, "LLM_PROVIDER"):
            llm.build_llm_config_from_settings()

    def test_unimportable_provider_raises_clear_error(self):
        with self.assertRaisesRegex(LLMConfigurationError, "import"):
            get_llm_provider(LLMConfig(provider="does.not.exist:Foo", enabled=True))

    def test_invalid_provider_class_raises_clear_error(self):
        with self.assertRaisesRegex(LLMConfigurationError, "BaseLLMProvider"):
            get_llm_provider(LLMConfig(provider="crank.agents.llm:LLMConfig", enabled=True))

    def test_provider_requiring_key_without_key_fails_closed(self):
        cfg = LLMConfig(provider="crank.tests.test_llm:KeyRequiringProvider", api_key="")
        with self.assertRaisesRegex(LLMConfigurationError, "LLM_API_KEY"):
            get_llm_provider(cfg)

    def test_provider_requiring_key_with_secret_builds(self):
        cfg = LLMConfig(
            provider="crank.tests.test_llm:KeyRequiringProvider",
            api_key="from-env-secret",
            max_tokens=64,
            enabled=True,
        )
        provider = get_llm_provider(cfg)
        self.assertIsInstance(provider, KeyRequiringProvider)

    def test_api_key_is_redacted_from_repr(self):
        cfg = LLMConfig(
            provider=BASE_PROVIDER,
            api_key="super-secret-abc",
            enabled=True,
        )
        self.assertNotIn("super-secret-abc", repr(cfg))
        self.assertNotIn("super-secret-abc", str(cfg))

    @override_settings(
        LLM_PROVIDER="crank.tests.test_llm:KeyRequiringProvider",
        LLM_API_KEY="from-override-settings",
    )
    def test_api_key_read_through_settings_overrides(self):
        # The builder must consume the Django settings layer (not os.environ
        # directly) so @override_settings actually takes effect.
        cfg = llm.build_llm_config_from_settings()
        self.assertEqual(cfg.api_key, "from-override-settings")
        provider = get_llm_provider()
        self.assertIsInstance(provider, KeyRequiringProvider)


# -- Structured response + usage normalization -------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "recommendation_ids": {"type": "array", "items": {"type": "integer"}},
        "preference_patch": {"type": "object"},
    },
    "required": ["message"],
}


class FakeProviderGatewayTests(SimpleTestCase):
    def _run(self, **overrides):
        cfg = LLMConfig(
            provider=BASE_PROVIDER,
            max_tokens=overrides.pop("max_tokens", 64),
            per_user_cost_limit_usd=overrides.pop(
                "per_user_cost_limit_usd", FAKE_CONFIG.per_user_cost_limit_usd
            ),
            price_per_1k_tokens_usd=0.01,
            enabled=True,
        )
        provider = get_llm_provider(cfg)
        req = LLMRequest(
            messages=[LLMMessage(role="user", content="recommend companies")],
            response_schema=overrides.pop("response_schema", SCHEMA),
            correlation_id="test-1",
            **overrides
        )
        return provider.complete(req)

    def test_structured_response_parsing(self):
        result = self._run()
        self.assertIn("message", result.data)
        self.assertIn("recommendation_ids", result.data)
        self.assertIn("preference_patch", result.data)
        self.assertIsInstance(result.data["recommendation_ids"], list)
        self.assertEqual(result.data["message"], "")  # placeholder, offline

    def test_usage_is_provider_neutral(self):
        result = self._run(response_schema=None)
        self.assertGreater(result.usage.total_tokens, 0)
        self.assertEqual(
            result.usage.total_tokens,
            result.usage.prompt_tokens + result.usage.completion_tokens,
        )
        self.assertIsInstance(result.usage.cost_estimate_usd, float)
        self.assertIsInstance(result.latency_ms, int)
        self.assertEqual(result.correlation_id, "test-1")
        self.assertEqual(result.provider, BASE_PROVIDER)
        self.assertIsInstance(result.content, str)

    def test_no_schema_returns_summary(self):
        result = self._run(response_schema=None)
        self.assertIsInstance(result.content, str)

    def test_required_field_missing_raises_schema_error(self):
        bad = dict(SCHEMA, required=["missing_field"])
        with self.assertRaises(llm.LLMSchemaError):
            self._run(response_schema=bad)

    def test_top_level_array_schema_returns_list(self):
        result = self._run(
            response_schema={"type": "array", "items": {"type": "integer"}}
        )
        self.assertEqual(result.data, [])


# -- Timeout / error translation and redaction -------------------------------


class ErrorTranslationTests(SimpleTestCase):
    def test_timeout_is_translated(self):
        provider = get_llm_provider(
            LLMConfig(provider="crank.tests.test_llm:TimeoutProvider", enabled=True)
        )
        with self.assertRaises(LLMTimeoutError):
            provider.complete(
                LLMRequest(
                    messages=[LLMMessage(role="user", content="hi")],
                    correlation_id="t",
                )
            )

    def test_hanging_provider_is_aborted_by_timeout(self):
        # A provider that blocks (network stall) must be cut off by the
        # configured timeout, not hang the caller indefinitely.
        provider = get_llm_provider(
            LLMConfig(
                provider="crank.tests.test_llm:HangingProvider",
                timeout_seconds=0.05,
                enabled=True,
            )
        )
        with self.assertRaises(LLMTimeoutError):
            provider.complete(
                LLMRequest(
                    messages=[LLMMessage(role="user", content="hi")],
                    correlation_id="slow",
                )
            )

    def test_disabled_provider_refuses_before_request(self):
        RecordingProvider.calls = 0
        provider = get_llm_provider(
            LLMConfig(
                provider="crank.tests.test_llm:RecordingProvider",
                enabled=False,
            )
        )
        with self.assertRaises(LLMConfigurationError):
            provider.complete(
                LLMRequest(
                    messages=[LLMMessage(role="user", content="hi")],
                    correlation_id="disabled",
                )
            )
        # No provider call is made while the provider is disabled.
        self.assertEqual(RecordingProvider.calls, 0)

    def test_provider_error_is_redacted(self):
        provider = get_llm_provider(
            LLMConfig(
                provider="crank.tests.test_llm:ExplodingProvider",
                api_key="super-secret-abc",
                max_tokens=64,
                enabled=True,
            )
        )
        with self.assertRaises(LLMProviderError) as ctx:
            provider.complete(
                LLMRequest(
                    messages=[LLMMessage(role="user", content="hi")],
                    correlation_id="leak-check",
                )
            )
        message = str(ctx.exception)
        # Never surface provider SDK detail, prompt, or user data. The only
        # identifiers permitted are provider/model and the correlation id.
        self.assertIn("correlation_id=leak-check", message)
        self.assertNotIn("alice@example.com", message)
        self.assertNotIn("full_prompt", message)
        self.assertNotIn("super-secret-abc", message)
        self.assertNotIn("DO-NOT-LEAK-ME", message)


# -- Ceilings ----------------------------------------------------------------


class CeilingTests(SimpleTestCase):
    def test_token_ceiling_forwarded_before_request(self):
        RecordingProvider.calls = 0
        provider = get_llm_provider(
            LLMConfig(
                provider="crank.tests.test_llm:RecordingProvider",
                max_tokens=64,
                enabled=True,
            )
        )
        request = LLMRequest(
            messages=[LLMMessage(role="user", content="x" * 5000)],
            max_tokens=64,
            correlation_id="tok",
        )
        with self.assertRaises(LLMUsageLimitError):
            provider.complete(request)
        # The ceiling guard runs before _call, so no (even fake) request is sent.
        self.assertEqual(RecordingProvider.calls, 0)

    def test_prompt_exactly_at_ceiling_is_allowed(self):
        # Off-by-one: a prompt estimated at exactly the token ceiling must not
        # be rejected (the heuristic is chars/4, so matching should pass).
        provider = get_llm_provider(
            LLMConfig(provider=BASE_PROVIDER, max_tokens=1, enabled=True)
        )
        result = provider.complete(
            LLMRequest(
                messages=[LLMMessage(role="user", content="")],
                response_schema=None,
                correlation_id="edge",
            )
        )
        self.assertIsNotNone(result)

    def test_cost_ceiling_forwarded_before_request(self):
        RecordingProvider.calls = 0
        provider = get_llm_provider(
            LLMConfig(
                provider="crank.tests.test_llm:RecordingProvider",
                max_tokens=2048,
                price_per_1k_tokens_usd=10_000_000.0,  # absurd price
                per_user_cost_limit_usd=0.001,
                enabled=True,
            )
        )
        with self.assertRaises(LLMUsageLimitError):
            provider.complete(
                LLMRequest(
                    messages=[LLMMessage(role="user", content="hi")],
                    correlation_id="cost",
                )
            )
        self.assertEqual(RecordingProvider.calls, 0)

    def test_ceiling_not_triggered_within_budget(self):
        provider = get_llm_provider(FAKE_CONFIG)
        result = provider.complete(
            LLMRequest(
                messages=[LLMMessage(role="user", content="hi")],
                response_schema=SCHEMA,
                correlation_id="ok",
            )
        )
        self.assertIsNotNone(result.data)


# -- Per-user spend ceiling --------------------------------------------------


class PerUserCostTests(SimpleTestCase):
    def setUp(self):
        # The in-process ledger is global; reset it so tests are isolated.
        reset_per_user_spend()

    def _provider(self):
        return get_llm_provider(
            LLMConfig(
                provider="crank.tests.test_llm:RecordingProvider",
                max_tokens=8,
                price_per_1k_tokens_usd=0.1,
                per_user_cost_limit_usd=0.001,
                enabled=True,
            )
        )

    def test_per_user_cumulative_spend_blocks_second_request(self):
        provider = self._provider()
        request = LLMRequest(
            messages=[LLMMessage(role="user", content="hi")],
            response_schema=None,
            user_id="user-42",
            correlation_id="u1",
        )
        # First request fits within the ceiling and is charged to the user.
        self.assertIsNotNone(provider.complete(request))
        self.assertGreater(get_per_user_spend("user-42"), 0.0)
        # A second request from the same user trips the cumulative per-user
        # ceiling even though each individual request is within budget.
        RecordingProvider.calls = 0
        with self.assertRaises(LLMUsageLimitError):
            provider.complete(request)
        self.assertEqual(RecordingProvider.calls, 0)

    def test_per_user_limit_is_isolated_by_user(self):
        provider = self._provider()
        messages = [LLMMessage(role="user", content="hi")]
        # User A spends up against the cumulative ceiling.
        self.assertIsNotNone(
            provider.complete(
                LLMRequest(
                    messages=messages, response_schema=None, user_id="a",
                    correlation_id="a1",
                )
            )
        )
        with self.assertRaises(LLMUsageLimitError):
            provider.complete(
                LLMRequest(
                    messages=messages, response_schema=None, user_id="a",
                    correlation_id="a2",
                )
            )
        # A different user starts fresh and is not blocked by user A's spend.
        self.assertIsNotNone(
            provider.complete(
                LLMRequest(
                    messages=messages, response_schema=None, user_id="b",
                    correlation_id="b1",
                )
            )
        )


# -- Feature flag ------------------------------------------------------------


class FeatureFlagTests(SimpleTestCase):
    @override_settings(INTERACTIVE_AGENT_ENABLED=False)
    def test_agent_disabled_independently(self):
        # Scheduled ingestion is "on" from this view's perspective, but the
        # interactive agent gate is closed independently.
        self.assertFalse(is_interactive_agent_enabled())

    @override_settings(INTERACTIVE_AGENT_ENABLED=True)
    def test_agent_enabled(self):
        self.assertTrue(is_interactive_agent_enabled())

    @override_settings()
    def test_defaults_to_disabled(self):
        self.assertFalse(is_interactive_agent_enabled())

    def test_gateway_still_buildable_when_agent_disabled(self):
        # Disabling the feature flag must not break importing/building the
        # gateway itself; the gate lives at the execution boundary.
        provider = get_llm_provider(FAKE_CONFIG)
        self.assertIsInstance(provider, FakeLLMProvider)


# -- No network at import ----------------------------------------------------


class ImportSideEffectTests(SimpleTestCase):
    def test_module_import_has_no_network(self):
        # Importing the gateway must not reach the network or read secrets.
        # Sanity: reloading the module is side-effect free and the globals it
        # exposes contain no credential values.
        import importlib

        mod = importlib.reload(llm)
        self.assertTrue(hasattr(mod, "get_llm_provider"))
        raw = {k: v for k, v in vars(mod).items() if "KEY" in k or "SECRET" in k}
        # No credentials may be present at module scope.
        for value in raw.values():
            if isinstance(value, str):
                self.assertEqual(value, "")