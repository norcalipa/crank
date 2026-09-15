# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Typed service errors for the job-search orchestration service.

These error types are the contract that callers (HTTP views, management
commands) can rely on. Timeout, provider failure, and cost-limit outcomes all
map to explicit subtypes of :class:`ProviderError`; malformed or untrusted
model output maps to subtypes of :class:`InvalidModelOutputError`.
"""


class JobSearchError(Exception):
    """Base class for all job-search orchestration errors."""


class ProviderError(JobSearchError):
    """The LLM provider failed to produce a usable completion."""


class ProviderTimeoutError(ProviderError):
    """The provider exceeded its enforcement/response timeout."""


class CostLimitError(ProviderError):
    """The provider call would exceed the configured token or cost limit."""


class InvalidModelOutputError(JobSearchError):
    """Model output failed schema validation and was rejected before use."""


class InvalidOrganizationReferenceError(InvalidModelOutputError):
    """The model cited an organization ID not returned by server-controlled tools.

    Cited organization IDs must be a strict subset of the IDs the bounded
    organization tools actually returned. Anything else is treated as a
    hallucinated reference and rejected without persistence.
    """


class InvalidPreferencePatchError(InvalidModelOutputError):
    """The model's preference patch is malformed or violates the preference schema."""


class PreferenceStaleError(JobSearchError):
    """A proposed preference patch lost an optimistic-concurrency check.

    The preference row changed between the turn's version capture (turn start)
    and patch application (after the provider reply), so applying the patch
    would overwrite a newer change the user made (or a reset) mid-turn. The
    patch is NOT applied; the transport maps this to a stable 409
    ``preference_stale`` envelope and the persisted user turn stays retryable.
    """


class PreferenceVersionUnavailableError(JobSearchError):
    """The preference baseline could not be captured at turn start.

    Fail-closed guard (issue #487): a writer preference port without a
    captured ``expected_modified`` baseline must never apply a proposed
    patch — doing so would silently restore the overwrite behavior the
    optimistic-concurrency check exists to prevent. The patch path aborts with
    this stable error instead; the transport maps it to the retryable 409
    ``preference_stale`` envelope.
    """


class ConversationClosedError(JobSearchError):
    """The conversation was reset or deleted while the turn was in flight.

    Raised by the per-turn lifecycle guard when a proposed preference patch
    is about to commit against a conversation that is no longer active (or no
    longer exists): the patch and the reply are both discarded. The transport
    maps this to the stable 409 ``conversation_closed`` envelope.
    """


def is_database_locked(exc: BaseException) -> bool:
    """Return True for transient backend lock-contention errors.

    Issue #487 review round 2 (MAJOR-2): on SQLite the guarded turn section
    and a concurrent lifecycle writer serialize at the single-writer lock;
    SQLite's deadlock avoidance fails one side fast with
    ``OperationalError: database is locked`` (MySQL equivalents: lock-wait
    timeout, deadlock detected). These are transient and retryable — the
    guarded turn maps them to the stable retryable 409 envelopes instead of
    a 500, and the lifecycle endpoints retry them in place.
    """
    try:
        from django.db import OperationalError
    except Exception:  # pragma: no cover - Django always available at runtime
        return False
    if not isinstance(exc, OperationalError):
        return False
    text = str(exc).lower()
    return (
        "locked" in text
        or "lock wait timeout" in text
        or "deadlock" in text
    )


class InvalidScoreSummaryRowError(JobSearchError):
    """A score-summary datasource returned a malformed/untyped row.

    Score rows are server-controlled but injectable (and may be faked in
    tests), so a row missing or mistyping ``organization_id``, ``score_type``
    or ``avg_score`` is surfaced as this typed error instead of a bare
    ``KeyError`` during rendering.
    """


class EchoReplyError(InvalidModelOutputError):
    """The assistant merely restated the user's message without tool work.

    Raised when the inventory is non-empty yet the reply is an unrooted echo
    of the user turn (no citations and no preference patch). This guards
    against a demo/echo provider leaking into production, where the assistant
    would be chatting but never surfacing a result card.
    """


class InvalidJobListingReferenceError(InvalidModelOutputError):
    """The model cited a job-listing ID not returned by server-controlled tools.

    Cited listing IDs must be a strict subset of the IDs the bounded
    job-listing tools actually returned. Anything else is treated as a
    hallucinated reference and rejected without persistence.
    """
