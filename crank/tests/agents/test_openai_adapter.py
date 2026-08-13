# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the OpenAI-compatible HTTP chat-completions adapter (OpenAIChatAdapter).

All tests use a fake HTTP transport — no live network calls are made in CI.
Covers: config fail-closed, structured output, usage normalization, error
sanitization, cost ceilings, timeout handling, and response parsing edges.
"""
import json

from django.test import SimpleTestCase, override_settings

ADAPTER_PATH = "crank.agents.llm:OpenAIChatAdapter"

SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "cited_organization_ids": {"type": "array", "items": {"type": "integer"}},
        "preference_patch": {"type": ["object", "null"]},
    },
    "required": ["message", "cited_organization_ids", "preference_patch"],
    "additionalProperties": False,
}


class FakeResponse:
    """Minimal requests.Response-like object for testing."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _llm():
    """Return the current crank.agents.llm module (survives reloads)."""
    import crank.agents.llm as _m
    return _m


def make_provider(
    transport=None,
    api_key="test-key-abc",
    model="gpt-4o",
    max_tokens=2048,
    per_user_cost_limit_usd=0.0,
    price_per_1k_tokens_usd=0.0,
    enabled=True,
    timeout_seconds=30,
):
    """Build an OpenAIChatAdapter with the given config and fake transport."""
    mod = _llm()
    cfg = mod.LLMConfig(
        provider=ADAPTER_PATH,
        model=model,
        api_key=api_key,
        max_tokens=max_tokens,
        per_user_cost_limit_usd=per_user_cost_limit_usd,
        price_per_1k_tokens_usd=price_per_1k_tokens_usd,
        enabled=enabled,
        timeout_seconds=timeout_seconds,
    )
    provider = mod.get_llm_provider(cfg)
    if transport is not None:
        provider._transport = transport
    return provider


def ok_response(content=None, data=None, usage=None):
    """Build a fake 200 response with a valid chat-completion payload."""
    if content is None and data is not None:
        content = json.dumps(data)
    elif content is None:
        content = '{"message": "ok", "cited_organization_ids": [], "preference_patch": null}'
    payload = {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage or {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }
    return FakeResponse(status_code=200, payload=payload)


class ConfigFailClosedTests(SimpleTestCase):
    """Missing API key or model must fail closed before any request is sent."""

    def test_missing_api_key_fails_closed(self):
        mod = _llm()
        with self.assertRaisesRegex(mod.LLMConfigurationError, "LLM_API_KEY"):
            make_provider(api_key="", model="gpt-4o")

    def test_missing_model_fails_closed(self):
        mod = _llm()
        with self.assertRaisesRegex(mod.LLMConfigurationError, "LLM_MODEL"):
            make_provider(api_key="test-key", model="")

    def test_empty_provider_fails_closed(self):
        mod = _llm()
        cfg = mod.LLMConfig(provider="", enabled=True)
        with self.assertRaisesRegex(mod.LLMConfigurationError, "LLM provider is not configured"):
            mod.FakeLLMProvider(cfg)

    @override_settings(LLM_PROVIDER=ADAPTER_PATH, LLM_API_KEY="", LLM_MODEL="")
    def test_settings_missing_key_and_model_fails_closed(self):
        mod = _llm()
        cfg = mod.build_llm_config_from_settings()
        with self.assertRaisesRegex(mod.LLMConfigurationError, "LLM_API_KEY"):
            mod.get_llm_provider(cfg)

    def test_disabled_provider_refuses_request(self):
        mod = _llm()
        provider = make_provider(
            api_key="test-key",
            model="gpt-4o",
            enabled=False,
        )
        with self.assertRaises(mod.LLMConfigurationError):
            provider.complete(
                mod.LLMRequest(
                    messages=[mod.LLMMessage(role="user", content="hi")],
                    correlation_id="disabled",
                )
            )

    def test_api_key_redacted_from_repr(self):
        mod = _llm()
        cfg = mod.LLMConfig(
            provider=ADAPTER_PATH,
            model="gpt-4o",
            api_key="super-secret-xyz",
            enabled=True,
        )
        self.assertNotIn("super-secret-xyz", repr(cfg))
        self.assertNotIn("super-secret-xyz", str(cfg))


class StructuredOutputTests(SimpleTestCase):
    """The adapter sends response_format and parses structured output."""

    def test_structured_response_parsed(self):
        mod = _llm()
        data = {
            "message": "I recommend Globex.",
            "cited_organization_ids": [1, 2],
            "preference_patch": None,
        }
        captured = {}

        def fake_transport(url, json=None, headers=None, timeout=None):
            captured["body"] = json
            captured["url"] = url
            captured["headers"] = headers
            return ok_response(data=data)

        provider = make_provider(transport=fake_transport)
        result = provider.complete(
            mod.LLMRequest(
                messages=[mod.LLMMessage(role="user", content="recommend companies")],
                response_schema=SCHEMA,
                correlation_id="struct-1",
            )
        )
        self.assertEqual(result.data["message"], "I recommend Globex.")
        self.assertEqual(result.data["cited_organization_ids"], [1, 2])
        self.assertIn("response_format", captured["body"])
        self.assertEqual(captured["body"]["response_format"]["type"], "json_schema")

    def test_no_schema_omits_response_format(self):
        mod = _llm()
        captured = {}

        def fake_transport(url, json=None, headers=None, timeout=None):
            captured["body"] = json
            return ok_response(content="Hello!")

        provider = make_provider(transport=fake_transport)
        result = provider.complete(
            mod.LLMRequest(
                messages=[mod.LLMMessage(role="user", content="hi")],
                response_schema=None,
                correlation_id="no-schema",
            )
        )
        self.assertNotIn("response_format", captured["body"])
        self.assertEqual(result.content, "Hello!")


class UsageNormalizationTests(SimpleTestCase):
    """Usage from the provider response is normalized provider-neutrally."""

    def test_usage_from_response(self):
        mod = _llm()
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        provider = make_provider(transport=lambda *a, **kw: ok_response(usage=usage))
        result = provider.complete(
            mod.LLMRequest(
                messages=[mod.LLMMessage(role="user", content="hi")],
                correlation_id="usage",
            )
        )
        self.assertEqual(result.usage.prompt_tokens, 100)
        self.assertEqual(result.usage.completion_tokens, 50)
        self.assertEqual(result.usage.total_tokens, 150)
        self.assertIsInstance(result.usage.cost_estimate_usd, float)
        self.assertIsInstance(result.latency_ms, int)
        self.assertEqual(result.correlation_id, "usage")
        self.assertEqual(result.provider, ADAPTER_PATH)

    def test_cost_calculated_from_price(self):
        mod = _llm()
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "total_tokens": 1500,
        }
        provider = make_provider(
            transport=lambda *a, **kw: ok_response(usage=usage),
            price_per_1k_tokens_usd=0.01,
        )
        result = provider.complete(
            mod.LLMRequest(
                messages=[mod.LLMMessage(role="user", content="hi")],
                correlation_id="cost",
            )
        )
        self.assertAlmostEqual(result.usage.cost_estimate_usd, 0.015, places=4)


class ErrorSanitizationTests(SimpleTestCase):
    """Provider errors must never leak prompts, keys, or user text."""

    def test_http_error_sanitized(self):
        mod = _llm()
        provider = make_provider(
            transport=lambda *a, **kw: FakeResponse(status_code=500, payload={})
        )
        with self.assertRaises(mod.LLMProviderError) as ctx:
            provider.complete(
                mod.LLMRequest(
                    messages=[mod.LLMMessage(role="user", content="secret user text")],
                    correlation_id="err-1",
                )
            )
        msg = str(ctx.exception)
        self.assertIn("correlation_id=err-1", msg)
        self.assertIn("HTTP 500", msg)
        self.assertNotIn("secret user text", msg)
        self.assertNotIn("test-key-abc", msg)

    def test_non_json_response_sanitized(self):
        mod = _llm()

        class BadResponse:
            status_code = 200

            def json(self):
                raise ValueError("not json")

        provider = make_provider(transport=lambda *a, **kw: BadResponse())
        with self.assertRaises(mod.LLMProviderError) as ctx:
            provider.complete(
                mod.LLMRequest(
                    messages=[mod.LLMMessage(role="user", content="hi")],
                    correlation_id="bad-json",
                )
            )
        self.assertIn("non-JSON", str(ctx.exception))

    def test_no_choices_in_response(self):
        mod = _llm()
        payload = {"choices": [], "usage": {}}
        provider = make_provider(
            transport=lambda *a, **kw: FakeResponse(status_code=200, payload=payload)
        )
        with self.assertRaises(mod.LLMProviderError) as ctx:
            provider.complete(
                mod.LLMRequest(
                    messages=[mod.LLMMessage(role="user", content="hi")],
                    correlation_id="no-choices",
                )
            )
        self.assertIn("no choices", str(ctx.exception))

    def test_headers_contain_bearer_key(self):
        captured = {}

        def fake_transport(url, json=None, headers=None, timeout=None):
            captured["headers"] = headers
            return ok_response()

        provider = make_provider(transport=fake_transport)
        mod = _llm()
        provider.complete(
            mod.LLMRequest(
                messages=[mod.LLMMessage(role="user", content="hi")],
                correlation_id="hdr",
            )
        )
        self.assertIn("Authorization", captured["headers"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key-abc")
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")


class TimeoutTests(SimpleTestCase):
    """The adapter uses the configured timeout on the HTTP call."""

    def test_timeout_passed_to_transport(self):
        mod = _llm()
        captured = {}

        def fake_transport(url, json=None, headers=None, timeout=None):
            captured["timeout"] = timeout
            return ok_response()

        provider = make_provider(
            transport=fake_transport,
            timeout_seconds=5.0,
        )
        provider.complete(
            mod.LLMRequest(
                messages=[mod.LLMMessage(role="user", content="hi")],
                correlation_id="to",
            )
        )
        self.assertEqual(captured["timeout"], 5.0)


class CostCeilingTests(SimpleTestCase):
    """Per-user cost ceilings are enforced before any HTTP call."""

    def setUp(self):
        _llm().reset_per_user_spend()

    def test_cost_ceiling_blocks_before_request(self):
        mod = _llm()
        calls = {"count": 0}

        def fake_transport(*a, **kw):
            calls["count"] += 1
            return ok_response()

        provider = make_provider(
            transport=fake_transport,
            price_per_1k_tokens_usd=10_000_000.0,
            per_user_cost_limit_usd=0.001,
        )
        with self.assertRaises(mod.LLMUsageLimitError):
            provider.complete(
                mod.LLMRequest(
                    messages=[mod.LLMMessage(role="user", content="hi")],
                    correlation_id="ceiling",
                )
            )
        self.assertEqual(calls["count"], 0)

    def test_token_ceiling_blocks_before_request(self):
        mod = _llm()
        calls = {"count": 0}

        def fake_transport(*a, **kw):
            calls["count"] += 1
            return ok_response()

        provider = make_provider(
            transport=fake_transport,
            max_tokens=10,
        )
        with self.assertRaises(mod.LLMUsageLimitError):
            provider.complete(
                mod.LLMRequest(
                    messages=[mod.LLMMessage(role="user", content="x" * 5000)],
                    max_tokens=10,
                    correlation_id="tok-ceiling",
                )
            )
        self.assertEqual(calls["count"], 0)


class RequestBuildingTests(SimpleTestCase):
    """The adapter builds correct request bodies and URLs."""

    def test_model_in_body(self):
        mod = _llm()
        captured = {}

        def fake_transport(url, json=None, headers=None, timeout=None):
            captured["body"] = json
            captured["url"] = url
            return ok_response()

        provider = make_provider(
            transport=fake_transport,
            model="gpt-4o-mini",
        )
        provider.complete(
            mod.LLMRequest(
                messages=[mod.LLMMessage(role="user", content="hi")],
                correlation_id="model-test",
            )
        )
        self.assertEqual(captured["body"]["model"], "gpt-4o-mini")

    @override_settings(LLM_API_BASE_URL="https://custom.example.com/v1")
    def test_custom_api_base_url(self):
        mod = _llm()
        captured = {}

        def fake_transport(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            return ok_response()

        provider = make_provider(transport=fake_transport)
        provider.complete(
            mod.LLMRequest(
                messages=[mod.LLMMessage(role="user", content="hi")],
                correlation_id="url-test",
            )
        )
        self.assertEqual(captured["url"], "https://custom.example.com/v1/chat/completions")


class ContentParsingTests(SimpleTestCase):
    """Content that is not JSON should still produce a string content."""

    def test_non_json_content_returns_string(self):
        mod = _llm()
        provider = make_provider(
            transport=lambda *a, **kw: ok_response(content="Just a plain string reply.")
        )
        result = provider.complete(
            mod.LLMRequest(
                messages=[mod.LLMMessage(role="user", content="hi")],
                correlation_id="plain",
            )
        )
        self.assertEqual(result.content, "Just a plain string reply.")
        self.assertIsNone(result.data)

    def test_json_content_returns_parsed_data(self):
        mod = _llm()
        data = {"message": "hi", "cited_organization_ids": [1], "preference_patch": None}
        provider = make_provider(
            transport=lambda *a, **kw: ok_response(data=data)
        )
        result = provider.complete(
            mod.LLMRequest(
                messages=[mod.LLMMessage(role="user", content="hi")],
                correlation_id="json",
            )
        )
        self.assertEqual(result.data["message"], "hi")
        self.assertEqual(result.data["cited_organization_ids"], [1])
