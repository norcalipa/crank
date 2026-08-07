# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Typed LLM provider gateway.

A small, provider-neutral interface for schema-capable LLM completions plus a
single concrete provider (``FakeLLMProvider``) that is selected through
settings. Call sites depend only on the ``LLMProvider`` protocol; provider SDK
calls live exclusively inside adapter implementations, so no provider SDK is
ever imported at a call site and no network I/O happens at module import.

Configuration is environment-backed and FAILS CLOSED: building a provider
without a selected provider, or with a provider that requires an API key while
none is configured, raises :class:`LLMConfigurationError` before any request is
sent. API keys are read only from environment-backed settings (never checked
in), and errors/logs never surface prompts, user data, or credentials.
"""

from __future__ import annotations

import abc
import asyncio
import concurrent.futures
import importlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Exceptions (provider-neutral, redaction-safe)
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base class for all gateway errors."""


class LLMConfigurationError(LLMError):
    """Raised when provider configuration is missing or invalid (fail closed)."""


class LLMTimeoutError(LLMError):
    """Raised when a provider request exceeds the configured timeout."""


class LLMUsageLimitError(LLMError):
    """Raised when a token or cost ceiling would be exceeded."""


class LLMProviderError(LLMError):
    """Raised for provider-side failures. The message is sanitized/redacted."""


class LLMSchemaError(LLMError):
    """Raised when a structured response cannot be parsed against the schema."""


# ---------------------------------------------------------------------------
# Per-user spend ledger + gateway executor (in-process, non-durable)
# ---------------------------------------------------------------------------

# In-memory cumulative spend per user, keyed by ``request.user_id``. This makes
# ``per_user_cost_limit_usd`` a genuine per-user *cumulative* ceiling for the
# lifetime of the gateway process. DOCUMENTED LIMITATION: it is not durable
# across restarts nor shared horizontally across processes/workers, so it is a
# best-effort per-process guard, not an authoritative accounting ledger.
_PER_USER_SPEND: Dict[str, float] = {}

# A lazy, shared executor lets ``complete()`` enforce a wall-clock timeout on a
# blocking synchronous SDK call without leaking a fresh thread per invocation
# or blocking on shutdown of a hung worker.
_executor: Optional["concurrent.futures.ThreadPoolExecutor"] = None
_executor_lock = threading.Lock()


def _gateway_executor() -> "concurrent.futures.ThreadPoolExecutor":
    """Return the shared (lazily created) gateway executor."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=8, thread_name_prefix="llm-gateway"
                )
    return _executor


def reset_per_user_spend() -> None:
    """Clear the in-process per-user spend ledger (mainly for tests)."""
    _PER_USER_SPEND.clear()


def get_per_user_spend(user_id: str) -> float:
    """Return cumulative estimated spend recorded so far for a single user."""
    return _PER_USER_SPEND.get(user_id, 0.0)


def _record_per_user_spend(user_id: Optional[str], cost_usd: float) -> None:
    if not user_id or not cost_usd:
        return
    _PER_USER_SPEND[user_id] = _PER_USER_SPEND.get(user_id, 0.0) + cost_usd


# ---------------------------------------------------------------------------
# Data types (provider-neutral)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMConfig:
    """Frozen, validated provider configuration.

    ``api_key`` is intentionally the *only* field read from a secret store and
    is never serialized or logged.
    """

    provider: str
    model: str = ""
    timeout_seconds: float = 30.0
    max_tokens: int = 2048
    per_user_cost_limit_usd: float = 0.0
    price_per_1k_tokens_usd: float = 0.0
    enabled: bool = True
    # ``repr=False``/``compare=False`` keep the secret out of ``__repr__``,
    # ``__str__``, equality diffs, and debug/error-page serialization.
    api_key: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class LLMUsage:
    """Normalized, provider-neutral usage counters."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_estimate_usd: float = 0.0


@dataclass(frozen=True)
class LLMMessage:
    """One chat message. Role/content only; never raw provider artifacts."""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMRequest:
    """A single completion request."""

    messages: List[LLMMessage]
    response_schema: Optional[Dict[str, Any]] = None
    max_tokens: Optional[int] = None
    cost_ceiling_usd: Optional[float] = None
    user_id: Optional[str] = None
    correlation_id: Optional[str] = None


@dataclass(frozen=True)
class LLMResult:
    """A normalized completion result.

    ``data`` holds the schema-validated structured payload (or ``None``).
    ``usage`` and ``latency_ms`` are always populated provider-neutrally.
    """

    content: str
    data: Any
    provider: str
    model: str
    usage: LLMUsage
    latency_ms: int
    correlation_id: Optional[str] = None


@runtime_checkable
class LLMProvider(Protocol):
    """Structural protocol for the gateway. Mockable without any SDK."""

    def complete(self, request: LLMRequest) -> LLMResult: ...


def _sanitized_exception(exc: Exception, config: "LLMConfig", correlation_id: Optional[str]) -> str:
    """Build a log-friendly message that never leaks SDK content.

    ``str(exc)`` from a provider SDK may embed prompts, responses, or
    credentials, so we deliberately discard it and keep only the exception
    type plus safe identifiers.
    """
    detail = type(exc).__name__
    return (
        f"LLM provider '{config.provider}' failed ({detail}); "
        f"correlation_id={correlation_id}"
    )


# ---------------------------------------------------------------------------
# Base provider: ceilings, timeout/error translation, usage normalization
# ---------------------------------------------------------------------------


class BaseLLMProvider(abc.ABC):
    """Shared gateway behavior. Subclasses implement ``_call`` (the only
    place a provider SDK is touched) and are responsible for their own config
    requirements via ``_validate_config``.
    """

    provider_name = "base"
    #: Providers that need an API key set this to True; the base class then
    #: fails closed with a clear configuration error when no key is configured.
    requires_api_key = False
    #: Providers that need a model set this to True and are validated.
    requires_model = False

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._validate_config()

    # -- config validation -------------------------------------------------

    def _validate_config(self) -> None:
        """Fail closed: reject missing/invalid production configuration."""
        if not self.config.provider:
            raise LLMConfigurationError(
                "LLM provider is not configured "
                "(set LLM_PROVIDER to a crank.agents.llm provider class)."
            )
        if self.requires_api_key and not self.config.api_key:
            raise LLMConfigurationError(
                f"LLM provider '{self.config.provider}' requires an API key, "
                "but LLM_API_KEY is missing. Refusing to start without a "
                "configured secret."
            )
        if self.requires_model and not self.config.model:
            raise LLMConfigurationError(
                f"LLM provider '{self.config.provider}' requires a model, "
                "but LLM_MODEL is not set."
            )

    # -- public gateway ----------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResult:
        """Run a completion with ceilings, timeout/error translation, and
        normalized usage. Raises LLMError subclasses; never the raw SDK error.
        """
        self._validate_enabled()
        self._enforce_ceilings(request)
        start = time.monotonic()
        try:
            raw = self._call_with_timeout(request)
        except LLMError:
            # Provider-neutral errors (schema, usage limits raised inside the
            # adapter) propagate unchanged; do not wrap them.
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise LLMTimeoutError(
                _sanitized_exception(exc, self.config, request.correlation_id)
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalized at the boundary
            raise LLMProviderError(
                _sanitized_exception(exc, self.config, request.correlation_id)
            ) from exc
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)

        usage = self._normalize_usage(raw)
        _record_per_user_spend(request.user_id, usage.cost_estimate_usd)
        content, data = self._parse_response(raw, request)
        return LLMResult(
            content=content,
            data=data,
            provider=self.config.provider,
            model=self.config.model,
            usage=usage,
            latency_ms=latency_ms,
            correlation_id=request.correlation_id,
        )

    def _validate_enabled(self) -> None:
        """Refuse to process requests when the provider is disabled."""
        if not self.config.enabled:
            raise LLMConfigurationError(
                "LLM provider is disabled (enabled=False). Refusing to process "
                "the request; enable the feature flag before calling."
            )

    def _call_with_timeout(self, request: LLMRequest) -> Any:
        """Run ``_call`` under the configured wall-clock timeout.

        A blocking synchronous SDK call cannot be preempted cooperatively, so
        the call runs on the shared gateway executor and ``future.result``
        enforces the deadline. On expiry we raise :class:`LLMTimeoutError`; the
        hung worker drains on its own and never blocks the caller.
        """
        timeout = self.config.timeout_seconds
        if not timeout or timeout <= 0:
            return self._call(request)
        future = _gateway_executor().submit(self._call, request)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            raise LLMTimeoutError(
                f"LLM provider '{self.config.provider}' did not respond within "
                f"{timeout}s (correlation_id={request.correlation_id})."
            ) from exc

    # -- ceilings (enforced before any request is sent) --------------------

    def _enforce_ceilings(self, request: LLMRequest) -> None:
        max_tokens = request.max_tokens or self.config.max_tokens
        estimated_prompt = self._estimate_prompt_tokens(request.messages)
        if estimated_prompt > max_tokens:
            raise LLMUsageLimitError(
                f"LLM request exceeds max_tokens={max_tokens} "
                f"(estimated prompt tokens={estimated_prompt})."
            )
        ceiling = request.cost_ceiling_usd
        if ceiling is None and self.config.per_user_cost_limit_usd:
            ceiling = self.config.per_user_cost_limit_usd
        if ceiling:
            estimate = self._estimate_cost(estimated_prompt, max_tokens)
            if estimate >= ceiling:
                raise LLMUsageLimitError(
                    f"LLM request exceeds cost ceiling ${ceiling:.6f} "
                    f"(estimated ${estimate:.6f})."
                )
            # Per-user cumulative spend must also stay within the ceiling.
            if request.user_id:
                spent = get_per_user_spend(request.user_id)
                if spent + estimate > ceiling:
                    raise LLMUsageLimitError(
                        f"LLM request would exceed per-user cost ceiling "
                        f"${ceiling:.6f}: user '{request.user_id}' has already "
                        f"spent ${spent:.6f} (projected ${spent + estimate:.6f})."
                    )

    # -- helpers to be customised per provider -----------------------------

    @abc.abstractmethod
    def _call(self, request: LLMRequest) -> Any:
        """Perform the actual provider call. THE ONLY spot SDK I/O may occur."""

    @abc.abstractmethod
    def _parse_response(self, raw: Any, request: LLMRequest):
        """Return (content, data) where data is schema-validated."""

    def _normalize_usage(self, raw: Any) -> LLMUsage:
        """Turn a provider's raw usage payload into an LLMUsage."""
        usage = {}
        if isinstance(raw, dict):
            usage = raw.get("usage") or {}
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        total = int(usage.get("total_tokens", 0) or prompt + completion)
        cost = float(usage.get("cost_estimate_usd", 0.0) or 0.0)
        if not cost:
            cost = self._estimate_cost(prompt, completion)
        return LLMUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cost_estimate_usd=round(cost, 6),
        )

    def _estimate_prompt_tokens(self, messages: List[LLMMessage]) -> int:
        # Rough, deterministic heuristic (~4 chars/token). Providers should
        # override with real tokenizers when accuracy matters.
        chars = sum(len(m.content or "") for m in messages) + sum(
            len(m.role or "") for m in messages
        )
        return max(1, chars // 4)

    def _estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            (prompt_tokens + completion_tokens)
            / 1000.0
            * self.config.price_per_1k_tokens_usd
        )


# ---------------------------------------------------------------------------
# The single selected provider implementation (no SDK, deterministic)
# ---------------------------------------------------------------------------


class FakeLLMProvider(BaseLLMProvider):
    """Deterministic, offline provider used for local/testing configuration.

    Satisfies the gateway contract without any network I/O or SDK dependency,
    which is what makes it an ideal injectable fake for call sites and the
    default (safe, disabled-until-configured) production fallback.
    """

    provider_name = "fake"
    requires_api_key = False
    requires_model = False

    def _call(self, request: LLMRequest) -> Dict[str, Any]:
        _ = self.config  # config is bound at construction
        payload = self._build_placeholder(request.response_schema)
        self._validate_payload(payload, request.response_schema)
        if isinstance(payload, dict) and payload.get("message"):
            content = payload["message"]
        else:
            content = json.dumps(payload)
        return {
            "content": content,
            "data": payload,
            "usage": self._fake_usage(request, content),
        }

    def _parse_response(self, raw: Any, request: LLMRequest):
        return raw.get("content"), raw.get("data")

    # -- helpers -----------------------------------------------------------

    def _fake_usage(self, request: LLMRequest, content: str) -> Dict[str, int]:
        prompt_tokens = self._estimate_prompt_tokens(request.messages)
        completion_tokens = max(1, len(content) // 4)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_estimate_usd": 0.0,  # filled in by _normalize_usage
        }

    def _build_placeholder(self, schema: Optional[Dict[str, Any]]) -> Any:
        if schema is None:
            # No schema requested: echo a safe summary of the request.
            return {"message": "ok"}
        schema_type = schema.get("type")
        if schema_type == "array":
            # Top-level array schema: satisfy it with an empty list.
            return []
        if schema_type not in (None, "object"):
            return {"message": "ok"}
        properties = schema.get("properties") or {}
        payload: Dict[str, Any] = {}
        for key, prop in properties.items():
            payload[key] = self._placeholder_for(prop)
        return payload

    def _placeholder_for(self, prop: Dict[str, Any]) -> Any:
        ptype = prop.get("type")
        if ptype in ("string",):
            return ""
        if ptype in ("number", "integer"):
            return 0
        if ptype == "boolean":
            return False
        if ptype == "array":
            return []
        if ptype == "object":
            return {}
        return None

    def _validate_payload(self, payload: Any, schema: Optional[Dict[str, Any]]) -> None:
        for key in (schema or {}).get("required", []):
            if key not in payload:
                raise LLMSchemaError(
                    f"Provider response missing required property '{key}'."
                )


# ---------------------------------------------------------------------------
# Configuration loading and provider selection
# ---------------------------------------------------------------------------


def build_llm_config_from_settings() -> LLMConfig:
    """Resolve LLM settings from Django settings + env-backed secrets.

    depends_on is intentionally just the settings object so this works in both
    Django and non-Django test contexts.
    """
    from django.conf import settings

    provider = (getattr(settings, "LLM_PROVIDER", "") or "").strip()
    if not provider:
        raise LLMConfigurationError(
            "LLM_PROVIDER is not configured. Set it to a crank.agents.llm "
            "provider class (e.g. 'crank.agents.llm:FakeLLMProvider') or "
            "disable the feature by leaving the feature flag off. FAILING "
            "CLOSED: no provider selected."
        )
    return LLMConfig(
        provider=provider,
        model=(getattr(settings, "LLM_MODEL", "") or "").strip(),
        timeout_seconds=float(getattr(settings, "LLM_TIMEOUT_SECONDS", 30.0)),
        max_tokens=int(getattr(settings, "LLM_MAX_TOKENS", 2048)),
        per_user_cost_limit_usd=float(
            getattr(settings, "LLM_PER_USER_COST_LIMIT_USD", 0.0)
        ),
        price_per_1k_tokens_usd=float(
            getattr(settings, "LLM_PRICE_PER_1K_TOKENS_USD", 0.0)
        ),
        enabled=bool(getattr(settings, "INTERACTIVE_AGENT_ENABLED", False)),
        # The API key is read through the settings layer, which itself loads it
        # from the environment (never from checked-in code). Reading it here via
        # ``getattr(settings, ...)`` (rather than ``os.environ`` directly) means
        # both real env resolution and ``@override_settings`` work in one path.
        api_key=(getattr(settings, "LLM_API_KEY", "") or "").strip(),
    )


def get_llm_provider(config: Optional[LLMConfig] = None) -> BaseLLMProvider:
    """Select and build the configured provider (fail closed)."""
    cfg = config if config is not None else build_llm_config_from_settings()
    klass = _import_provider(cfg.provider)
    return klass(cfg)


def _import_provider(dotted: str) -> type:
    dotted = (dotted or "").strip()
    if not dotted:
        raise LLMConfigurationError(
            "LLM_PROVIDER is not configured. Set it to a crank.agents.llm "
            "provider class (e.g. 'crank.agents.llm:FakeLLMProvider'). "
            "FAILING CLOSED: no provider selected."
        )
    if ":" in dotted:
        module_name, _, class_name = dotted.partition(":")
    else:
        module_name, _, class_name = dotted.rpartition(".")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - normalized configuration error
        raise LLMConfigurationError(
            f"Could not import LLM provider module '{module_name}'."
        ) from exc
    klass = getattr(module, class_name, None)
    if not (isinstance(klass, type) and issubclass(klass, BaseLLMProvider)):
        raise LLMConfigurationError(
            f"'{dotted}' is not a BaseLLMProvider subclass."
        )
    return klass


def is_interactive_agent_enabled() -> bool:
    """Independent feature flag for the interactive job-search agent.

    Disabling this does NOT touch scheduled ingestion, which has its own
    lifecycle. This makes Interactive Agent execution independently switchable.
    """
    from django.conf import settings

    return bool(getattr(settings, "INTERACTIVE_AGENT_ENABLED", False))
