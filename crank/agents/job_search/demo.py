# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Job-search conversation transport: demo provider (Phase 1).

The orchestration service (:mod:`crank.agents.job_search.service`) implements
the real bounded conversation turn. This module provides the **transport
layer's** provider interface used by the authenticated chat view
(:mod:`crank.views.job_search`).

The view never calls an LLM directly; it delegates a turn to
:class:`JobSearchService`, which in turn asks a provider for a reply.

``run_turn`` returns ``(assistant_message_text, preferences_changed)``. The
provider is selected by the ``JOB_SEARCH_PROVIDER`` setting; the default is the
bounded :class:`DemoJobSearchProvider`, which simulates a multi-turn flow so
the authenticated transport and UI can be exercised end-to-end while the
LLM-gateway adapter that drives the real orchestrator is still being wired up.
It never performs web access, never emits URLs, and treats every user turn as
untrusted text.
"""
from __future__ import annotations

import logging
import os

from django.conf import settings

from crank.agents.job_search import quality
from crank.agents.job_search.errors import (
    ConversationClosedError as _OrchestratorConversationClosed,
    CostLimitError as _OrchestratorCostLimit,
    InvalidModelOutputError as _OrchestratorInvalidOutput,
    PreferenceStaleError as _OrchestratorPreferenceStale,
    PreferenceVersionUnavailableError as _OrchestratorPreferenceVersionUnavailable,
    ProviderTimeoutError as _OrchestratorTimeout,
)
from crank.checks import is_non_dev_environment

logger = logging.getLogger("crank.agents.job_search")

__all__ = [
    "AssistantUnavailable",
    "DemoJobSearchProvider",
    "JobSearchService",
    "JobSearchServiceError",
    "ServiceConversationClosed",
    "ServiceCostLimit",
    "ServiceInvalidOutput",
    "ServicePreferenceStale",
    "ServicePreferenceVersionUnavailable",
    "ServiceTimeout",
]


class JobSearchServiceError(Exception):
    """Raised when a provider cannot produce a valid reply for a turn."""


class AssistantUnavailable(JobSearchServiceError):
    """The assistant is disabled or not configured for this environment."""


class ServiceTimeout(JobSearchServiceError):
    """The provider exceeded its response timeout."""


class ServiceCostLimit(JobSearchServiceError):
    """The provider call would exceed the configured cost/token limit."""


class ServiceInvalidOutput(JobSearchServiceError):
    """The provider output failed schema validation."""


class ServicePreferenceStale(JobSearchServiceError):
    """A proposed preference patch lost its optimistic-concurrency check.

    The preference row changed while the turn was in flight (the user edited
    or reset preferences, or another request patched them), so the patch was
    NOT applied. The view maps this to a stable 409 ``preference_stale``
    envelope; the persisted user turn remains retryable (issue #487).
    """


class ServicePreferenceVersionUnavailable(JobSearchServiceError):
    """The preference baseline could not be captured at turn start.

    Fail-closed guard (issue #487 review, MAJOR-4): a writer preference port
    without a captured ``expected_modified`` baseline never applies a proposed
    patch. The view maps this to the same stable, retryable 409
    ``preference_stale`` envelope; the persisted user turn remains retryable.
    """


class ServiceConversationClosed(JobSearchServiceError):
    """The conversation was reset or deleted while the turn was in flight.

    Raised when the lifecycle guard aborts a proposed preference patch whose
    conversation is no longer active (issue #487 review, MAJOR-2). The view
    maps this to the stable 409 ``conversation_closed`` envelope; the
    persisted user turn remains retryable.
    """


class DemoJobSearchProvider:
    """Bounded simulated provider for the chat transport.

    Keeps a trivial turn count against the conversation so follow-ups in the
    same session behave like a real multi-turn flow. Preference changes are
    simulated when the user's message mentions a preference dimension. This is
    the offline/test double used until the real LLM-gateway adapter lands.
    """

    PREFERENCE_HINTS = (
        "compensation",
        "salary",
        "remote",
        "location",
        "culture",
        "funding",
        "funding round",
        "vesting",
        "rto",
        "in-office",
        "hybrid",
        "industry",
    )
    ASSISTANT_NAME = "CRank Career Assistant"

    def generate_reply(self, *, conversation, user_message):
        """Return ``(reply_text, preferences_changed, results)`` for a single turn."""
        text = (user_message or "").strip()
        turn_number = conversation.messages.filter(role="user").count()
        changed = any(hint in text.lower() for hint in self.PREFERENCE_HINTS)

        if turn_number <= 1:
            reply = (
                "Thanks! I can help you find organizations here on CRank. "
                "Tell me what matters most to you — for example compensation, "
                "remote/in-office policy, funding stage, or culture — and I'll "
                "point you at organizations that fit.\n\nThe full "
                "property-based matching flow is still being wired up, so this "
                "phase returns a canned recommendation until the conversation "
                "orchestration is wired to a live provider."
            )
        else:
            reply = (
                f"Got it. Based on what you've shared so far, I'd recommend "
                f"reviewing the organizations that match '{text}'. I'll keep "
                f"refining the match as we go."
            )
        return reply, changed, None


def _build_provider():
    """Resolve the configured provider (demo by default, orchestrator for production).

    The demo provider is an offline/test double only: it never touches a real
    inventory and can't ground an anti-echo guard. In a non-dev environment it
    is disabled outright so an ungrounded demo can never serve production
    traffic (issue #423).

    Production (orchestrator) fails closed when:
    - ``INTERACTIVE_AGENT_ENABLED`` is False
    - ``LLM_PROVIDER`` is not configured or the selected provider raises
      ``LLMConfigurationError`` (missing API key, missing model, etc.)
    - Any other ``LLMError`` prevents the provider from starting.

    There is never a silent fallback to the demo provider — if the operator
    selected ``orchestrator``, they must also configure the LLM gateway.
    """
    # E2E failure hook (issue #491): deterministic, default-off injection point
    # so the browser suite can exercise the provider-outage recovery contract
    # against a real server. Double-gated: it only arms when the operator
    # exports CRANK_E2E_PROVIDER_FAILURE=1 *and* the process is running in a
    # dev environment (``is_non_dev_environment`` is False), so a stray env var
    # can never force an outage in staging or production. Prod/staging cases
    # proving the env var is inert there live in crank/tests/agents/test_providers.py.
    if (
        os.environ.get("CRANK_E2E_PROVIDER_FAILURE") == "1"
        and not is_non_dev_environment()
    ):
        raise AssistantUnavailable(
            "The assistant is not available right now. Please try again later."
        )
    name = getattr(settings, "JOB_SEARCH_PROVIDER", "demo")
    if name == "demo":
        if is_non_dev_environment():
            raise AssistantUnavailable(
                "The job-search assistant is not available. "
                "Please try again later or contact support."
            )
        return DemoJobSearchProvider()
    if name == "orchestrator":
        # Fail closed: refuse to start if the interactive agent feature flag is off.
        if not getattr(settings, "INTERACTIVE_AGENT_ENABLED", False):
            raise AssistantUnavailable(
                "The job-search assistant is not available. "
                "Please try again later or contact support."
            )
        from crank.agents.job_search.providers import OrchestratorJobSearchProvider
        try:
            return OrchestratorJobSearchProvider()
        except Exception as exc:
            # Surface config errors (e.g. missing API key) as an unavailable
            # error so the view returns a stable 503, not a 500 crash.
            raise AssistantUnavailable(
                "The job-search assistant is not available. "
                "Please try again later or contact support."
            ) from exc
    raise AssistantUnavailable(
        f"Unknown JOB_SEARCH_PROVIDER: {name!r}"
    )


class JobSearchService:
    """Thin service layer between the chat view and the provider."""

    def __init__(self, provider=None):
        self.provider = provider or _build_provider()

    def run_turn(self, *, conversation, user_message, persist_reply=None):
        """Run one turn; returns ``(reply_text, preferences_changed, results)``.

        ``results`` is an optional :class:`StructuredResults` (or ``None``).
        ``persist_reply``, when given, is forwarded to the provider so the
        assistant reply is persisted INSIDE the same database transaction as
        the lifecycle guard's row claim and the preference patch (issue #487
        review round 2, MAJOR-1) — one commit boundary for the whole turn.
        Providers whose ``generate_reply`` does not accept the hook (legacy
        or demo providers) are called without it, and the view persists the
        reply itself in a self-contained transaction as before.

        Raises :class:`JobSearchServiceError` when the provider fails so the
        view can return a stable 500 without persisting a duplicate message.
        """
        try:
            import inspect

            params = inspect.signature(self.provider.generate_reply).parameters
            accepts_hook = "persist_reply" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            if accepts_hook:
                reply_text, changed, results = self.provider.generate_reply(
                    conversation=conversation,
                    user_message=user_message,
                    persist_reply=self._bound_persist_hook(persist_reply),
                )
            else:
                # Legacy provider signature: no guarded persistence hook; the
                # view's post-turn transaction handles the reply.
                reply_text, changed, results = self.provider.generate_reply(
                    conversation=conversation, user_message=user_message
                )
        except JobSearchServiceError:
            # Already a typed service error (e.g. from generate_reply);
            # let it propagate without re-wrapping.
            raise
        except _OrchestratorTimeout as exc:
            logger.error(
                "job_search provider timeout conversation=%s",
                getattr(conversation, "pk", None),
            )
            raise ServiceTimeout(
                "The assistant took too long to respond. Please retry."
            ) from exc
        except _OrchestratorCostLimit as exc:
            logger.error(
                "job_search cost limit conversation=%s",
                getattr(conversation, "pk", None),
            )
            raise ServiceCostLimit(
                "The assistant has reached its usage limit. "
                "Please try again later."
            ) from exc
        except _OrchestratorInvalidOutput as exc:
            logger.error(
                "job_search invalid model output conversation=%s",
                getattr(conversation, "pk", None),
            )
            raise ServiceInvalidOutput(
                "The assistant produced an unexpected response. "
                "Please try again."
            ) from exc
        except _OrchestratorPreferenceStale as exc:
            logger.error(
                "job_search stale preference patch conversation=%s",
                getattr(conversation, "pk", None),
            )
            raise ServicePreferenceStale(
                "Your preferences changed while the assistant was responding. "
                "Please retry."
            ) from exc
        except _OrchestratorConversationClosed as exc:
            logger.info(
                "job_search conversation closed mid-turn conversation=%s",
                getattr(conversation, "pk", None),
            )
            raise ServiceConversationClosed(
                "This conversation was reset or deleted while the assistant "
                "was responding."
            ) from exc
        except _OrchestratorPreferenceVersionUnavailable as exc:
            logger.error(
                "job_search preference baseline unavailable conversation=%s",
                getattr(conversation, "pk", None),
            )
            raise ServicePreferenceVersionUnavailable(
                "Your preferences could not be verified while the assistant "
                "was responding. Please retry."
            ) from exc
        except Exception as exc:  # provider failure -> stable service error
            logger.error(
                "job_search service error conversation=%s provider=%s error_type=%s",
                conversation.pk,
                type(self.provider).__name__,
                type(exc).__name__,
            )
            raise JobSearchServiceError(
                "provider failed to produce a response"
            ) from exc

        # Bound the reply deterministically; never trust unbounded output.
        max_len = getattr(settings, "JOB_SEARCH_RESPONSE_MAX_LEN", 8000)
        if len(reply_text) > max_len:
            reply_text = reply_text[:max_len]
        # Anti-echo guarantee for the configured demo path (issue #423). The
        # orchestrator guards the server-grounded provider internally; the
        # demo provider is guarded here so it can never serve a reply that
        # merely restates the user's turn.
        if isinstance(self.provider, DemoJobSearchProvider) and quality.is_echo(
            user_message or "", reply_text
        ):
            raise JobSearchServiceError(
                "The assistant could not produce a grounded reply. "
                "Please try again later or contact support."
            )
        return (reply_text or "").strip(), bool(changed), results

    @staticmethod
    def _bound_persist_hook(persist_reply):
        """Wrap the reply hook with the transport's length bound (round 3).

        The hook persists the reply INSIDE the orchestrator's guarded
        transaction — before ``run_turn`` bounds the returned text — so the
        transport bound (``JOB_SEARCH_RESPONSE_MAX_LEN``) must be applied to
        the message before persistence: bound at the source of truth, not at
        view response time (PR #501 review round 3, MINOR). Only an over-bound
        message is truncated; a within-bound message persists verbatim.
        ``AssistantCompletion``'s fixed 8000-character schema ceiling still
        applies above this configurable transport cap.
        """
        if persist_reply is None:
            return None

        def bounded(*, reply_text, results, preferences_changed):
            max_len = getattr(settings, "JOB_SEARCH_RESPONSE_MAX_LEN", 8000)
            if reply_text is not None and len(reply_text) > max_len:
                reply_text = reply_text[:max_len]
            return persist_reply(
                reply_text=reply_text,
                results=results,
                preferences_changed=preferences_changed,
            )

        return bounded
