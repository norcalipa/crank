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

    class Meta:
        app_label = "crank"
        ordering = ["created", "id"]

    def __str__(self):  # noqa: D105
        return "JobSearchMessage({}) role={} conversation={}".format(
            self.pk, self.role, self.conversation_id
        )