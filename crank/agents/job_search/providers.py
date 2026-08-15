# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Production job-search providers that wire the orchestrator to the LLM gateway.

This module bridges two abstraction layers:

* :class:`LLMGateway` — a :class:`~crank.agents.job_search.gateway.ProviderGateway`
  implementation that delegates to a :class:`~crank.agents.llm.LLMProvider` for
  actual completions.  It translates ``ModelRequest`` → ``LLMRequest`` and
  ``LLMResult`` → ``GatewayResponse``, mapping LLM gateway errors to the typed
  job-search error hierarchy.

* :class:`OrchestratorJobSearchProvider` — a transport-layer provider
  (compatible with :class:`~crank.agents.job_search.demo.JobSearchService`)
  that implements ``generate_reply`` by running the real
  :class:`~crank.agents.job_search.service.JobSearchOrchestrator`.  It maps the
  orchestrator's :class:`~crank.agents.job_search.service.OrchestratorResult`
  to the ``(reply_text, preferences_changed)`` transport contract.

Configuration errors (e.g. missing API key) are surfaced as friendly chat
messages via :class:`~crank.agents.job_search.demo.JobSearchServiceError`
rather than a 500 crash.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, TypeAlias, Union

from crank.agents.job_search.errors import (
    CostLimitError,
    InvalidModelOutputError,
    InvalidOrganizationReferenceError,
    InvalidPreferencePatchError,
    JobSearchError,
    ProviderError,
    ProviderTimeoutError,
)
from crank.agents.job_search.gateway import (
    GatewayResponse,
    ModelRequest,
    ProviderGateway,
)
from crank.agents.job_search.service import JobSearchOrchestrator
from crank.agents.llm import BaseLLMProvider, get_llm_provider

logger = logging.getLogger("crank.agents.job_search")

#: Sentinel cache key used when no owner could be resolved (``conversation.owner``
#: is ``None``). Kept distinct from any real user object so the ``None`` owner
#: builds its own orchestrator rather than reusing a real user's.
_NO_OWNER = object()

#: Stable cache key identifying an owner. A resolved, saved user maps to
#: ``(model_label, pk)`` so the same row materialized as different instances
#: shares one orchestrator; an unsaved user maps to a unique per-instance token;
#: and an unresolved owner maps to the :data:`_NO_OWNER` sentinel object.
_OwnerKey: TypeAlias = Union[object, tuple[str, Any]]

__all__ = [
    "LLMGateway",
    "OrchestratorJobSearchProvider",
]


# ---------------------------------------------------------------------------
# LLM-backed ProviderGateway
# ---------------------------------------------------------------------------


class LLMGateway(ProviderGateway):
    """ProviderGateway adapter that delegates to a :class:`LLMProvider`.

    Translates ``ModelRequest`` → ``LLMRequest``, runs the completion, and
    maps ``LLMResult`` → ``GatewayResponse`` and LLM errors → typed
    job-search errors. This is the only place where the two error hierarchies
    are bridged.
    """

    def __init__(self, provider: BaseLLMProvider | None = None) -> None:
        self._provider = provider or get_llm_provider()

    def complete(self, request: ModelRequest) -> GatewayResponse:
        """Execute a completion and return a :class:`GatewayResponse`.

        Raises typed job-search errors (``ProviderError``,
        ``ProviderTimeoutError``, ``CostLimitError``) on failure.
        """
        # Import lazily so that if the llm module was reloaded (e.g. by tests)
        # we catch the current exception classes.
        from crank.agents.llm import (
            LLMConfigurationError as _ConfigErr,
        )
        from crank.agents.llm import (
            LLMError as _BaseErr,
        )
        from crank.agents.llm import (
            LLMMessage,
            LLMRequest,
        )
        from crank.agents.llm import (
            LLMProviderError as _ProviderErr,
        )
        from crank.agents.llm import (
            LLMTimeoutError as _TimeoutErr,
        )
        from crank.agents.llm import (
            LLMUsageLimitError as _UsageErr,
        )

        messages = []
        if request.system:
            messages.append(LLMMessage(role="system", content=request.system))
        for msg in request.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system" and messages and messages[0].role == "system":
                messages[0] = LLMMessage(
                    role="system",
                    content=messages[0].content + "\n\n" + content,
                )
                continue
            messages.append(LLMMessage(role=role, content=content))

        llm_request = LLMRequest(
            messages=messages,
            response_schema=_RESPONSE_SCHEMA,
            max_tokens=request.max_tokens,
            correlation_id=request.prompt_id,
        )

        try:
            result = self._provider.complete(llm_request)
        except _TimeoutErr as exc:
            raise ProviderTimeoutError(str(exc)) from exc
        except _UsageErr as exc:
            raise CostLimitError(str(exc)) from exc
        except _ConfigErr as exc:
            raise ProviderError(str(exc)) from exc
        except _ProviderErr as exc:
            raise ProviderError(str(exc)) from exc
        except _BaseErr as exc:
            raise ProviderError(str(exc)) from exc

        usage = {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "total_tokens": result.usage.total_tokens,
            "output_tokens": result.usage.completion_tokens,
            "estimated_cost_usd": result.usage.cost_estimate_usd,
        }
        return GatewayResponse(text=result.content, usage=usage)

    def close(self) -> None:
        """Release resources. The LLM provider has no resources to close."""


# ---------------------------------------------------------------------------
# Response schema for the orchestrator's structured output
# ---------------------------------------------------------------------------

#: JSON schema sent to the LLM so it returns a structured ``AssistantCompletion``.
#: This mirrors :class:`crank.agents.job_search.types.AssistantCompletion`.
_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "cited_organization_ids": {
            "type": "array",
            "items": {"type": "integer"},
        },
        "cited_job_listing_ids": {
            "type": "array",
            "items": {"type": "integer"},
        },
        "preference_patch": {
            "type": ["object", "null"],
        },
    },
    "required": ["message", "cited_organization_ids", "cited_job_listing_ids", "preference_patch"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Orchestrator-backed transport provider
# ---------------------------------------------------------------------------


class _PreferenceServiceAdapter:
    """Owner-scoped adapter over the production preference store.

    Wires :mod:`crank.services.preferences` — the same store the preference
    editor uses — so chat preference changes are validated against the
    version-1 schema and persisted for the conversation owner.
    ``apply_patch`` returns whether the stored document actually changed, so
    ``preferences_changed`` accurately reflects persistence.
    """

    def __init__(self, user: Any) -> None:
        self._user = user

    def validate_patch(self, patch: dict[str, Any]) -> None:
        from crank.services.preferences import validate_patch

        validate_patch(patch)

    def apply_patch(self, patch: dict[str, Any]) -> bool:
        from crank.services.preferences import apply_patch_to_user

        result = apply_patch_to_user(self._user, patch)
        return bool(result.get("changed", False))


class _NullPreferenceService:
    """Fallback preference service used when no owner can be resolved.

    Only reachable when ``conversation.owner`` is unavailable (so there is no
    user to persist to). ``apply_patch`` returns ``False`` to signal nothing
    was applied, so ``preferences_changed`` accurately reports degradation
    rather than silently claiming a change persisted.
    """

    def validate_patch(self, patch: dict[str, Any]) -> None:
        # No user is available to persist to, so there is nothing meaningful
        # to validate against. The orchestrator already bounds the patch shape.
        pass

    def apply_patch(self, patch: dict[str, Any]) -> bool:
        return False


def _matches_for_user(user: Any):
    """Return the orchestrator ``match_service`` for *user*.

    The returned callable matches the orchestrator's ``match_service``
    contract — ``f(user, *, limit=...)`` returning
    ``{"job_matches": [...], "organization_matches": [...]}`` — by delegating
    to the real, preference-grounded matching engine
    (:func:`crank.agents.job_search.tools.get_matches_for_user`). Allowing the
    injection side-effect keeps tests deterministic without live ORM/network.
    """

    def _match(user: Any, *, limit: int | None = None) -> dict[str, Any]:
        from crank.agents.job_search.tools import get_matches_for_user

        return get_matches_for_user(user, limit=limit)

    return _match


class OrchestratorJobSearchProvider:
    """Transport provider that delegates to :class:`JobSearchOrchestrator`.

    Implements ``generate_reply(conversation, user_message)`` by building the
    orchestrator's inputs from the conversation model, running a single turn,
    and mapping the :class:`OrchestratorResult` to the
    ``(reply_text, preferences_changed)`` transport contract.

    Configuration errors (e.g. missing API key) are caught and surfaced as
    friendly error messages rather than crashes.
    """

    def __init__(
        self,
        *,
        gateway: ProviderGateway | None = None,
        preference_service: Any | None = None,
        preference_service_factory: callable | None = None,
        match_service: callable | None = None,
        orchestrator: JobSearchOrchestrator | None = None,
        orchestrator_factory: callable | None = None,
    ) -> None:
        if orchestrator is not None:
            # A fully-wired orchestrator was injected (tests / callers); use it
            # verbatim rather than re-wiring the owner services. If the injected
            # orchestrator is owner-bound (has a non-None ``_user``) it can only
            # ever serve that owner: a request for any other owner fails closed
            # rather than leaking the first owner's wiring.
            self._fixed_orchestrator = orchestrator
            self._fixed_orchestrator_owner = getattr(orchestrator, "_user", None)
            self._gateway = None
            self._preference_service = None
            self._preference_service_factory = None
            self._match_service = None
            self._orchestrator_factory = None
        else:
            self._fixed_orchestrator = None
            self._fixed_orchestrator_owner = None
            # Resolve the gateway eagerly so startup errors (missing API key,
            # missing model, etc.) fail closed at provider construction (in
            # ``_build_provider``), not on the first chat turn. When an
            # ``orchestrator_factory`` is injected, the caller owns the full
            # orchestrator wiring (including any gateway), so no gateway is
            # resolved here.
            self._gateway = (
                None
                if orchestrator_factory is not None
                else (gateway or LLMGateway())
            )
            self._preference_service = preference_service
            self._preference_service_factory = preference_service_factory
            self._match_service = match_service
            self._orchestrator_factory = orchestrator_factory
        # Lazy per-owner orchestrator cache, an LRU (bounded) so a shared,
        # long-lived provider never retains every owner seen. Keys are the
        # stable owner identity (``_owner_key``), so the same DB user reuses a
        # single orchestrator even when re-materialized as a new instance, and
        # evicted entries release their owner-bound orchestrators for
        # collection. Each distinct owner (or ``_NO_OWNER`` for an unresolved
        # owner) still gets its own freshly-wired orchestrator.
        self._orchestrators: OrderedDict[_OwnerKey, JobSearchOrchestrator] = OrderedDict()

    # -- owner resolution & safe injection ---------------------------------

    @staticmethod
    def _owner_key(user: Any) -> _OwnerKey:
        """Map *user* to a stable, hashable cache key.

        A saved user (one with a primary key) maps to ``(model_label, pk)`` so
        the same database row materialized as different Python instances shares
        one orchestrator instead of building duplicates. An unsaved user (no
        pk) has no stable identity, so it maps to a unique per-instance token
        and is never shared across instances. ``None`` maps to the
        :data:`_NO_OWNER` sentinel.
        """
        if user is None:
            return _NO_OWNER
        pk = getattr(user, "pk", None)
        if pk is not None:
            try:
                label = type(user)._meta.label
            except AttributeError:
                # Non-ORM owner (e.g. a simple test double): fall back to the
                # class name, which is still stable for a given row.
                label = type(user).__name__
            return (label, pk)
        return ("__unsaved__", id(user))

    @staticmethod
    def _reject_mismatched_owner(
        requested: Any,
        bound: Any,
        what: str,
    ) -> None:
        """Fail closed when an injected, owner-bound artifact is reused.

        *bound* is the owner an injected orchestrator/preference service is
        tied to (its ``_user``), or ``None`` when it is owner-neutral. Owner-
        neutral injection may be reused across owners; an owner-bound artifact
        may only serve its own owner. A request for a different owner (or an
        unresolved ``None`` owner) raises instead of leaking the bound owner's
        wiring or data.
        """
        if bound is None:
            return
        if requested is None or not OrchestratorJobSearchProvider._same_owner(
            requested, bound
        ):
            raise ValueError(
                f"the injected {what} is owner-scoped to {bound!r} and cannot "
                f"serve {requested!r}; inject a per-owner factory instead"
            )

    @staticmethod
    def _same_owner(a: Any, b: Any) -> bool:
        """Return True when *a* and *b* resolve to the same owner identity.

        Two different instances materializing the same database user row compare
        equal (same ``(label, pk)`` key); ``None`` only equals ``None``; and
        unsaved instances are only identical to themselves.
        """
        return (
            OrchestratorJobSearchProvider._owner_key(a)
            == OrchestratorJobSearchProvider._owner_key(b)
        )

    def _resolve_preference_service(self, user: Any):
        """Return the preference service to wire for *user*."""
        factory = getattr(self, "_preference_service_factory", None)
        if factory is not None:
            return factory(user)
        injected = getattr(self, "_preference_service", None)
        if injected is None:
            # Wire the real owner-scoped preference store. The null fallback is
            # only reachable when no owner could be resolved.
            return (
                _PreferenceServiceAdapter(user)
                if user is not None
                else _NullPreferenceService()
            )
        bound = getattr(injected, "_user", None)
        self._reject_mismatched_owner(user, bound, "preference service")
        return injected

    def _cache_orchestrator(
        self, key: _OwnerKey, orchestrator: JobSearchOrchestrator
    ) -> None:
        """Insert *orchestrator*, evicting the least-recently-used owner."""
        self._orchestrators[key] = orchestrator
        self._orchestrators.move_to_end(key)
        while len(self._orchestrators) > self._cache_size:
            self._orchestrators.popitem(last=False)

    _cache_size: int = 128

    def _ensure_orchestrator(self, user: Any) -> JobSearchOrchestrator:
        """Return the orchestrator for *user*, wiring preferences and matches.

        Because the owner service wiring depends on ``conversation.owner``
        (only known at ``generate_reply`` time), the orchestrator is built on
        first use and cached **per owner**. Reusing one provider across
        different conversation owners builds — and returns — an orchestrator
        wired for each owner, never the first owner's. The owner's real,
        preference-grounded ``PreferenceService`` and ``match_service`` are
        wired so saved preferences feed matching and preference patches persist.
        """
        if self._fixed_orchestrator is not None:
            self._reject_mismatched_owner(
                user, self._fixed_orchestrator_owner, "orchestrator"
            )
            return self._fixed_orchestrator
        key = self._owner_key(user)
        cached = self._orchestrators.get(key)
        if cached is not None:
            self._orchestrators.move_to_end(key)
            return cached
        factory = getattr(self, "_orchestrator_factory", None)
        if factory is not None:
            orchestrator = factory(user)
        else:
            pref = self._resolve_preference_service(user)
            match = self._match_service or _matches_for_user(user)
            orchestrator = JobSearchOrchestrator(
                gateway=self._gateway,
                preference_service=pref,
                user=user,
                match_service=match,
            )
        self._cache_orchestrator(key, orchestrator)
        return orchestrator

    @staticmethod
    def _resolve_user(conversation):
        """Return the conversation owner, or ``None`` when unavailable."""
        return getattr(conversation, "owner", None)

    def generate_reply(self, *, conversation, user_message):
        """Return ``(reply_text, preferences_changed, results)`` for a turn.

        Passes ``conversation.owner`` through to the orchestrator so saved
        preferences are loaded and matches are preference-grounded. Raises
        :class:`~crank.agents.job_search.demo.JobSearchServiceError` for
        configuration errors so the view returns a friendly message.
        """
        user = self._resolve_user(conversation)
        orchestrator = self._ensure_orchestrator(user)
        try:
            history = self._build_conversation_history(conversation)
            preference_markdown = self._get_preference_markdown(conversation)
        except Exception as exc:
            logger.error(
                "orchestrator provider history error conversation=%s error_type=%s",
                getattr(conversation, "pk", None),
                type(exc).__name__,
            )
            raise

        try:
            result = orchestrator.run(
                user_prompt=user_message or "",
                conversation=history,
                preference_markdown=preference_markdown,
            )
        except (ProviderError, ProviderTimeoutError, CostLimitError) as exc:
            logger.error(
                "orchestrator provider failure error_type=%s",
                type(exc).__name__,
            )
            raise
        except (
            InvalidModelOutputError,
            InvalidOrganizationReferenceError,
            InvalidPreferencePatchError,
        ) as exc:
            logger.error(
                "orchestrator validation failure error_type=%s",
                type(exc).__name__,
            )
            raise
        except JobSearchError as exc:
            logger.error(
                "orchestrator job_search error error_type=%s",
                type(exc).__name__,
            )
            raise
        except Exception as exc:
            # Surface config errors (e.g. missing-key) as a friendly message.
            from crank.agents.job_search.demo import JobSearchServiceError
            from crank.agents.llm import LLMConfigurationError as _ConfigErr
            if isinstance(exc, _ConfigErr):
                raise JobSearchServiceError(
                    "The job-search assistant is not configured. "
                    "Please try again later or contact support."
                ) from exc
            raise

        return result.message, result.preferences_changed, result.results

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _build_conversation_history(conversation) -> list[dict[str, str]]:
        """Extract prior messages from the Django conversation model."""
        messages: list[dict[str, str]] = []
        for msg in conversation.messages.order_by("created", "id"):
            messages.append({
                "role": msg.role,
                "content": msg.content or "",
            })
        return messages

    @staticmethod
    def _get_preference_markdown(conversation) -> str:
        """Fetch the user's preference markdown, if available."""
        try:
            from crank.models.preference import UserPreference
            pref = UserPreference.objects.filter(user_id=conversation.owner_id).first()
            if pref:
                return pref.preferences_markdown or ""
        except Exception:  # noqa: BLE001
            # Preference model/table may not be ready in test contexts.
            logger.debug(
                "Preference lookup unavailable for user=%s",
                getattr(conversation, "owner_id", None),
            )
        return ""
