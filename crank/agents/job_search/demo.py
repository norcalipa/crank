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

from django.conf import settings

logger = logging.getLogger("crank.agents.job_search")

__all__ = [
    "DemoJobSearchProvider",
    "JobSearchService",
    "JobSearchServiceError",
]


class JobSearchServiceError(Exception):
    """Raised when a provider cannot produce a valid reply for a turn."""


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
        """Return ``(reply_text, preferences_changed)`` for a single turn."""
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
        return reply, changed


def _build_provider():
    """Resolve the configured provider (demo by default, orchestrator for production)."""
    name = getattr(settings, "JOB_SEARCH_PROVIDER", "demo")
    if name == "demo":
        return DemoJobSearchProvider()
    if name == "orchestrator":
        from crank.agents.job_search.providers import OrchestratorJobSearchProvider
        try:
            return OrchestratorJobSearchProvider()
        except Exception as exc:
            # Surface config errors (e.g. missing API key) as a friendly service
            # error so the view returns a stable message, not a 500 crash.
            raise JobSearchServiceError(
                "The job-search assistant could not be started. "
                "Please try again later or contact support."
            ) from exc
    raise JobSearchServiceError(f"Unknown JOB_SEARCH_PROVIDER: {name!r}")


class JobSearchService:
    """Thin service layer between the chat view and the provider."""

    def __init__(self, provider=None):
        self.provider = provider or _build_provider()

    def run_turn(self, *, conversation, user_message):
        """Run one turn; returns ``(reply_text, preferences_changed)``.

        Raises :class:`JobSearchServiceError` when the provider fails so the
        view can return a stable 500 without persisting a duplicate message.
        """
        try:
            reply_text, changed = self.provider.generate_reply(
                conversation=conversation, user_message=user_message
            )
        except Exception as exc:  # provider failure -> stable service error
            logger.error(
                "job_search service error conversation=%s provider=%s error_type=%s",
                conversation.pk,
                type(self.provider).__name__,
                type(exc).__name__,
            )
            raise JobSearchServiceError("provider failed to produce a response") from exc

        # Bound the reply deterministically; never trust unbounded output.
        max_len = getattr(settings, "JOB_SEARCH_RESPONSE_MAX_LEN", 8000)
        if len(reply_text) > max_len:
            reply_text = reply_text[:max_len]
        return (reply_text or "").strip(), bool(changed)
