# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel


class Conversation(TimeStampedModel):
    """A multi-turn conversation/session owned by a single authenticated user.

    Conversations provide server-owned state for multi-turn flows so clients
    never control the history. Retention policy: conversations are kept while
    `status` is active and until `retention_until` if set (NULL means keep
    until archived or the owning user is deleted). Deleting the owning user
    cascades to all conversation records.

    Retention note: only user role + content is persisted here; provider
    reasoning, hidden prompts, and API credentials are never stored.
    """

    class Status(models.TextChoices):
        ACTIVE = "A", _("Active")
        ARCHIVED = "R", _("Archived")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversations",
        verbose_name=_("user"),
    )
    title = models.CharField(
        max_length=200,
        default="",
        blank=True,
        verbose_name=_("title"),
    )
    status = models.CharField(
        max_length=1,
        choices=Status.choices,
        default=Status.ACTIVE,
        verbose_name=_("status"),
    )
    retention_until = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("retention until"),
        help_text=_(
            "Retain the conversation until this timestamp; NULL keeps it until "
            "it is archived or the owning user is deleted."
        ),
    )

    def __str__(self):
        # Never expose message content in the model representation.
        return _("Conversation %(id)s for %(username)s") % {
            "id": self.pk,
            "username": self.user.username,
        }

    class Meta:
        app_label = "crank"
        ordering = ["-modified"]
        verbose_name = _("conversation")
        verbose_name_plural = _("conversations")
        indexes = [
            models.Index(fields=["user", "-modified"], name="crank_conv_user_modified_idx"),
            models.Index(fields=["user", "status"], name="crank_conv_user_status_idx"),
        ]


class Message(TimeStampedModel):
    """An ordered message within a conversation.

    `order` provides a stable, server-controlled sequence key within a
    conversation; a unique `(conversation, order)` constraint prevents
    accidental reordering/duplicates. Roles are limited to the known set so a
    client can never inject an arbitrary role.
    """

    class Role(models.TextChoices):
        USER = "U", _("User")
        ASSISTANT = "A", _("Assistant")

    class Status(models.TextChoices):
        SENT = "S", _("Sent")
        ERROR = "E", _("Error")

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
        verbose_name=_("conversation"),
    )
    role = models.CharField(
        max_length=1,
        choices=Role.choices,
        verbose_name=_("role"),
    )
    content = models.TextField(verbose_name=_("content"))
    order = models.PositiveIntegerField(verbose_name=_("order"))
    status = models.CharField(
        max_length=1,
        choices=Status.choices,
        default=Status.SENT,
        verbose_name=_("status"),
    )

    def __str__(self):
        # Never expose message content in the model representation.
        return _("Message %(order)s of conversation %(conversation_id)s") % {
            "order": self.order,
            "conversation_id": self.conversation_id,
        }

    class Meta:
        app_label = "crank"
        ordering = ["order", "id"]
        verbose_name = _("message")
        verbose_name_plural = _("messages")
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "order"],
                name="crank_message_unique_conv_order",
                violation_error_message=_(
                    "Each message must have a unique order within its conversation."
                ),
            )
        ]
        indexes = [
            models.Index(fields=["conversation", "order"], name="crank_msg_conv_order_idx"),
        ]