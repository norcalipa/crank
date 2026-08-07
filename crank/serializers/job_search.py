# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
Serializers and DTO helpers for the authenticated job-search chat transport.

Inbound payloads are validated through REST Framework serializers (size limits,
required fields, idempotency key shape). Outbound payloads are built by the
``serialize_*`` helpers so the HTTP shape is stable and testable independent of
view/DRF plumbing.

Security note: message/assistant text is always emitted as plain text and the
UI renders it with ``textContent`` semantics, never as HTML.
"""
from rest_framework import serializers

from django.conf import settings
from django.utils import timezone


class MessageSubmitSerializer(serializers.Serializer):
    """Validates a client submission of a new user turn."""

    content = serializers.CharField(allow_blank=False, trim_whitespace=True)
    idempotency_key = serializers.UUIDField(format="hex_verbose")

    def validate_content(self, value):  # noqa: D102
        max_len = getattr(settings, "JOB_SEARCH_MESSAGE_MAX_LEN", 4000)
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Message content is required.")
        if len(value) > max_len:
            raise serializers.ValidationError(
                "Message content must be {} characters or fewer.".format(max_len)
            )
        return value


class ConversationCreateSerializer(serializers.Serializer):
    """Validates the body of a create/resume conversation request."""

    create_new = serializers.BooleanField(required=False, default=False)


def serialize_message(message):
    """Return the stable JSON shape for a single message."""
    return {
        "id": message.pk,
        "role": message.role,
        "content": message.content,
        "preferences_changed": message.preferences_changed,
        "created": message.created.isoformat() if message.created else None,
    }


def serialize_conversation(conversation):
    """Return the stable JSON shape for a conversation plus retained history."""
    retention = getattr(settings, "JOB_SEARCH_MESSAGES_RETENTION", 50)
    messages = list(
        reversed(
            list(conversation.messages.order_by("-created", "-id")[:retention])
        )
    )
    return {
        "id": conversation.pk,
        "active": conversation.active,
        "created": conversation.created.isoformat() if conversation.created else None,
        "modified": conversation.modified.isoformat() if conversation.modified else None,
        "messages": [serialize_message(m) for m in messages],
        "preferences_changed": any(m.preferences_changed for m in messages),
    }


def serialize_user_profile(conversation):
    """Return preference/ownership metadata surfaced to the UI.

    ``preferences_changed`` lets the UI disclose that processing a turn updated
    the user's stored preferences without leaking their content.
    """
    retention = getattr(settings, "JOB_SEARCH_MESSAGES_RETENTION", 50)
    messages = list(
        reversed(
            list(conversation.messages.order_by("-created", "-id")[:retention])
        )
    )
    return {
        "conversation_id": conversation.pk,
        "preferences_changed": any(m.preferences_changed for m in messages),
        "message_count": conversation.messages.count(),
        "modified": conversation.modified.isoformat() if conversation.modified else None,
        "exported_at": timezone.now().isoformat(),
    }