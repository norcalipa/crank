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
import json
import logging

from rest_framework import serializers

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("crank.serializers.job_search")


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


def _serialize_results(results_obj):
    """Return a JSON-serialisable dict for a StructuredResults instance, or None."""
    if results_obj is None:
        return None
    try:
        return results_obj.to_json_dict()
    except Exception:
        logger.error("failed to serialize results block", exc_info=True)
        return None


def _parse_results_json(raw: str):
    """Parse a persisted results_json string into a dict, or return None."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.error("failed to parse results_json", exc_info=True)
        return None
    return parsed


def serialize_message(message, assistant_keys=None, turn_states=None):
    """Return the stable JSON shape for a single message.

    User messages additionally carry ``idempotency_key``, ``delivery_state``
    and ``retry_available`` (issue #458). The key is an owner-scoped random
    UUID the client needs to retry a failed turn without duplicating it; the
    delivery state drives the failed-turn UI; ``retry_available`` tells the
    client whether the per-turn attempt cap still allows a retry. For rows
    written before turn anchors existed the state is derived at read time
    from the presence of a matching assistant reply, which keeps legacy
    semantics identical to the pre-#458 behavior.
    """
    results = None
    if getattr(message, "results_json", ""):
        results = _parse_results_json(message.results_json)
    data = {
        "id": message.pk,
        "role": message.role,
        "content": message.content,
        "preferences_changed": message.preferences_changed,
        "created": message.created.isoformat() if message.created else None,
        "results": results,
    }
    if message.role == "user":
        state, retry_available = _user_delivery_state(
            message, assistant_keys, turn_states
        )
        data["idempotency_key"] = message.idempotency_key
        data["delivery_state"] = state
        data["retry_available"] = retry_available
    return data


def _user_delivery_state(message, assistant_keys=None, turn_states=None):
    """Return ``(delivery_state, retry_available)`` for a user turn.

    ``turn_states`` maps turn keys to their anchor state (authoritative for
    rows written after issue #458). Rows persisted before turn anchors existed
    derive ``completed`` when an assistant reply with the same key exists, else
    ``failed`` — matching the implicit pre-#458 semantics — and a derived
    ``failed`` stays retryable (its first retry creates the anchor). User rows
    without an idempotency key predate key-based turns and are reported as
    ``completed`` (there is nothing retriable for them).
    """
    key = message.idempotency_key
    if not key:
        return "completed", False
    if turn_states and key in turn_states:
        state, retry_available = turn_states[key]
        return state, retry_available
    if assistant_keys and key in assistant_keys:
        return "completed", False
    return "failed", True


def _turn_states(conversation):
    """Map each anchored turn key to its serialized state (issue #458).

    The anchor row is the single source of truth for post-#458 turns;
    ``retry_available`` is only true while the turn is failed and its
    ``attempt_count`` is still under ``JOB_SEARCH_TURN_MAX_ATTEMPTS``.
    """
    attempt_cap = max(1, int(getattr(settings, "JOB_SEARCH_TURN_MAX_ATTEMPTS", 5)))
    states = {}
    for turn in conversation.turns.all():
        if turn.delivery_state == "failed":
            states[turn.turn_key] = (
                "failed",
                turn.attempt_count < attempt_cap,
            )
        else:
            states[turn.turn_key] = (turn.delivery_state, False)
    return states


def _ordered_messages(messages):
    """Return messages in stable transcript order (issue #458).

    A keyed assistant reply is emitted immediately after its user turn even
    when the reply row was persisted later (a successful retry), so a retry
    never relocates the question and reloads never reorder history. Keyless
    rows (pre-key era) and orphan replies keep their timestamp order.
    """
    user_keys = {
        m.idempotency_key for m in messages
        if m.role == "user" and m.idempotency_key
    }
    reply_by_key = {
        m.idempotency_key: m for m in messages
        if m.role == "assistant" and m.idempotency_key
    }
    ordered = []
    for message in messages:
        if message.role == "user" and message.idempotency_key:
            ordered.append(message)
            reply = reply_by_key.pop(message.idempotency_key, None)
            if reply is not None:
                ordered.append(reply)
        elif (
            message.role == "assistant"
            and message.idempotency_key
            and message.idempotency_key in user_keys
        ):
            # Already emitted right after its user turn above.
            continue
        else:
            ordered.append(message)
    return ordered


def serialize_conversation(conversation):
    """Return the stable JSON shape for a conversation plus retained history."""
    retention = getattr(settings, "JOB_SEARCH_MESSAGES_RETENTION", 50)
    messages = list(
        reversed(
            list(conversation.messages.order_by("-created", "-id")[:retention])
        )
    )
    assistant_keys = {
        m.idempotency_key
        for m in messages
        if m.role == "assistant" and m.idempotency_key
    }
    turn_states = _turn_states(conversation)
    ordered = _ordered_messages(messages)
    return {
        "id": conversation.pk,
        "active": conversation.active,
        "created": conversation.created.isoformat() if conversation.created else None,
        "modified": conversation.modified.isoformat() if conversation.modified else None,
        "messages": [
            serialize_message(m, assistant_keys, turn_states) for m in ordered
        ],
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
