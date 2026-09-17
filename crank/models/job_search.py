# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
Minimal, owner-scoped persistence for the job-search chat transport.

These models belong to Phase 1 of the authenticated job-search chat. They hold
only what the API/UI layer needs to surface conversation history across page
loads and to make retries idempotent. Provider internals, LLM reasoning, and
the canonical preference document live elsewhere (preference/conversation
orchestration) and are intentionally not duplicated here.

Every row is bound to a single owner via a foreign key. Views must never trust
a client-supplied user id; they derive the owner from ``request.user`` and scope
queries so a user can never see another user's conversation.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel


class JobSearchConversation(TimeStampedModel):
    """An owner-scoped chat between the user and the job-search assistant."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_search_conversations",
    )
    active = models.BooleanField(default=True, help_text="Closed conversations are ignored on resume.")
    helpfulness_gap_emitted = models.BooleanField(
        default=False,
        help_text="True once the one-time job_search_helpfulness_gap telemetry event "
                  "has been emitted for this conversation. Set atomically so "
                  "concurrent submissions emit the gap signal exactly once.",
    )

    class Meta:
        app_label = "crank"
        ordering = ["-created", "-id"]

    def __str__(self):  # noqa: D105
        return "JobSearchConversation({}) owner={}".format(self.pk, self.owner_id)


class JobSearchMessage(TimeStampedModel):
    """One turn in a conversation, either from the user or the assistant."""

    class Role(models.TextChoices):
        USER = "user", _("User")
        ASSISTANT = "assistant", _("Assistant")

    conversation = models.ForeignKey(
        JobSearchConversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField(help_text="Message text. Rendered as text, never as raw HTML.")
    preferences_changed = models.BooleanField(
        default=False,
        help_text="True when processing this turn changed the user's stored preferences.",
    )
    idempotency_key = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Client-supplied key so retried submissions do not duplicate messages.",
    )
    results_json = models.TextField(
        blank=True,
        default="",
        help_text="Bounded JSON of citation-validated structured results (job/org cards), "
                  "persisted so history renders identically on reload.",
    )

    class Meta:
        app_label = "crank"
        ordering = ["created", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "idempotency_key", "role"],
                condition=~Q(idempotency_key=""),
                name="unique_jobsearch_message_idempotency",
            ),
        ]

    def __str__(self):  # noqa: D105
        return "JobSearchMessage({}) role={} conversation={}".format(
            self.pk, self.role, self.conversation_id
        )


class JobSearchTurn(TimeStampedModel):
    """Anchor row owning one user turn's delivery lifecycle (issue #458).

    Exactly one row exists per ``(conversation, turn_key)`` via an
    **unconditional** unique constraint, so the anchor can be created with
    ``get_or_create`` under concurrency on every supported backend — MySQL
    included, where Django never emits the message-level partial unique
    constraint (``unique_jobsearch_message_idempotency``, W036). The anchor
    is therefore the authoritative apply-once guard: concurrent first
    submissions serialize on this row (``select_for_update`` after the
    ``get_or_create`` race), at most one request runs the provider per claim,
    retries are bounded by ``JOB_SEARCH_TURN_MAX_ATTEMPTS``, and a ``pending``
    claim whose lease expired is recovered (marked failed-and-retryable)
    because its worker can no longer be running.
    """

    conversation = models.ForeignKey(
        JobSearchConversation,
        on_delete=models.CASCADE,
        related_name="turns",
    )
    turn_key = models.CharField(
        max_length=64,
        db_index=True,
        help_text="The user message's idempotency key; identifies the turn.",
    )

    class DeliveryState(models.TextChoices):
        """Delivery state of the turn."""

        PENDING = "pending", _("Pending")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")

    class FailureCode(models.TextChoices):
        """Stable failure codes; mirror the typed error envelope's ``error.type``
        plus the two recovery-only codes (``worker_interrupted`` for a reaped
        stale claim, ``conversation_gone`` for a reply quarantined because the
        conversation was reset/deleted mid-turn)."""

        ASSISTANT_UNAVAILABLE = "assistant_unavailable", _("Assistant unavailable")
        PROVIDER_TIMEOUT = "provider_timeout", _("Provider timeout")
        COST_LIMIT = "cost_limit", _("Cost limit")
        INVALID_OUTPUT = "invalid_output", _("Invalid output")
        SERVICE_ERROR = "service_error", _("Service error")
        UNEXPECTED_ERROR = "unexpected_error", _("Unexpected error")
        WORKER_INTERRUPTED = "worker_interrupted", _("Worker interrupted")
        CONVERSATION_GONE = "conversation_gone", _("Conversation gone")

    delivery_state = models.CharField(
        max_length=16,
        choices=DeliveryState.choices,
        default=DeliveryState.PENDING,
        db_index=True,
        help_text="Claim state of the turn: pending (a worker owns the claim), "
                  "completed (assistant reply persisted), or failed (retryable "
                  "while attempts remain).",
    )
    failure_code = models.CharField(
        max_length=32,
        choices=FailureCode.choices,
        blank=True,
        default="",
        help_text="Stable failure code recorded when the turn ends failed; matches "
                  "the typed error envelope's ``error.type`` (plus the two "
                  "recovery-only codes).",
    )
    attempt_count = models.PositiveIntegerField(
        default=0,
        db_index=True,
        help_text="Provider executions started for this turn; bounded by "
                  "JOB_SEARCH_TURN_MAX_ATTEMPTS so poisoned turns cannot loop.",
    )
    lease_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the current pending claim may be taken over or reaped; "
                  "set just above the longest legitimate provider run.",
    )

    class Meta:
        app_label = "crank"
        ordering = ["created", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "turn_key"],
                name="unique_jobsearch_turn_per_conversation",
            ),
        ]

    def __str__(self):  # noqa: D105
        return "JobSearchTurn({}) conversation={} state={}".format(
            self.pk, self.conversation_id, self.delivery_state
        )
