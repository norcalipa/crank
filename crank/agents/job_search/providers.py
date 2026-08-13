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
        "preference_patch": {
            "type": ["object", "null"],
        },
    },
    "required": ["message", "cited_organization_ids", "preference_patch"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Orchestrator-backed transport provider
# ---------------------------------------------------------------------------


class _NullPreferenceService:
    """Minimal preference service stub used when no real one is wired.

    Validates patches pass-through and always reports no change.  Production
    wires the real preference service from ``crank.services.preferences``.
    """

    def validate_patch(self, patch: dict[str, Any]) -> None:
        # Accept any patch shape; the orchestrator already validated it via
        # AssistantCompletion schema. The real preference service does deeper
        # validation.
        pass

    def apply_patch(self, patch: dict[str, Any]) -> bool:
        return False


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
        orchestrator: JobSearchOrchestrator | None = None,
    ) -> None:
        if orchestrator is not None:
            self._orchestrator = orchestrator
        else:
            gw = gateway or LLMGateway()
            pref = preference_service or _NullPreferenceService()
            self._orchestrator = JobSearchOrchestrator(
                gateway=gw,
                preference_service=pref,
            )

    def generate_reply(self, *, conversation, user_message):
        """Return ``(reply_text, preferences_changed)`` for a single turn.

        Raises :class:`~crank.agents.job_search.demo.JobSearchServiceError`
        for configuration errors so the view returns a friendly message.
        """
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
            result = self._orchestrator.run(
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

        return result.message, result.preferences_changed

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
