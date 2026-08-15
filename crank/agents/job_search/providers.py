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
from typing import Any

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
        match_service: callable | None = None,
        orchestrator: JobSearchOrchestrator | None = None,
    ) -> None:
        if orchestrator is not None:
            # A fully-wired orchestrator was injected (tests / callers); use it
            # verbatim rather than re-wiring the owner services.
            self._fixed_orchestrator = orchestrator
            self._gateway = None
            self._preference_service = None
            self._match_service = None
        else:
            self._fixed_orchestrator = None
            # Resolve the gateway eagerly so configuration errors (missing API
            # key, missing model, etc.) fail closed at provider construction
            # (in ``_build_provider``), not on the first chat turn.
            self._gateway = gateway or LLMGateway()
            self._preference_service = preference_service
            self._match_service = match_service
        # Built lazily for the conversation owner once we know *who* is chatting.
        self._orchestrator: JobSearchOrchestrator | None = None

    def _ensure_orchestrator(self, user: Any) -> JobSearchOrchestrator:
        """Return the orchestrator for *user*, wiring preferences and matches.

        Because the owner service wiring depends on ``conversation.owner``
        (only known at ``generate_reply`` time), the orchestrator is built on
        first use and cached. The owner's real, preference-grounded
        ``PreferenceService`` and ``match_service`` are wired so saved
        preferences feed matching and preference patches persist.
        """
        if self._fixed_orchestrator is not None:
            return self._fixed_orchestrator
        if self._orchestrator is not None:
            return self._orchestrator
        pref = self._preference_service
        if pref is None:
            # Wire the real owner-scoped preference store in production. The
            # null fallback is only reachable when no owner could be resolved.
            pref = (
                _PreferenceServiceAdapter(user)
                if user is not None
                else _NullPreferenceService()
            )
        match = self._match_service or _matches_for_user(user)
        self._orchestrator = JobSearchOrchestrator(
            gateway=self._gateway,
            preference_service=pref,
            user=user,
            match_service=match,
        )
        return self._orchestrator

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
