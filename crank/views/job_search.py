# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
Authenticated job-search chat transport (Phase 1).

The server derives the user identity from the authenticated session; clients
never supply a user id. Every conversation query is scoped to ``request.user``
so guessing another user's conversation id returns 404, never their data.

Behavior contract (mirrored by the test suite):

* Anonymous users are rejected (redirect/401).
* Conversations are owner-scoped; cross-user access returns 404.
* POST bodies are CSRF-checked, size-limited, and validated via serializers.
* Submissions are idempotent on ``idempotency_key``: a retried submission does
  not duplicate the persisted user message or machine reply.
* Provider/service failures return a stable 500 and durable retries.
* Per-user/IP request limits apply; leaning on them returns a stable 429.
* A correlation id is echoed back in ``X-Request-ID`` on every response.
* Message and assistant content is never logged and is always rendered as text.
"""
import json
import logging
import uuid

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crank.agents.job_search.demo import JobSearchService, JobSearchServiceError
from crank.models import JobSearchConversation, JobSearchMessage
from crank.serializers.job_search import (
    ConversationCreateSerializer,
    MessageSubmitSerializer,
    serialize_conversation,
    serialize_message,
    serialize_user_profile,
)

logger = logging.getLogger("crank.job_search")


def _request_id(request):
    """Return the client-supplied or generated correlation id for a request."""
    rid = request.META.get("HTTP_X_REQUEST_ID") or ""
    if not rid:
        rid = uuid.uuid4().hex
    return rid[:128]


def _error(request, status, error_type, message, request_id):
    """Return the stable error envelope used by every failing request."""
    return JsonResponse(
        {"error": {"type": error_type, "message": message, "request_id": request_id}},
        status=status,
        headers={"X-Request-ID": request_id},
    )


def _body(request, request_id):
    """Parse the JSON request body, enforcing a payload size limit.

    Returning a tuple ``(payload, error_response)`` where either ``payload`` is
    a dict or ``error_response`` is a JsonResponse to return to the client.
    """
    try:
        raw = request.body
    except Exception:  # pragma: no cover - defensive
        raw = b""
    max_bytes = getattr(settings, "JOB_SEARCH_REQUEST_MAX_BYTES", 65536)
    if len(raw) > max_bytes:
        return None, _error(
            request, 413, "payload_too_large",
            "Request body exceeds the allowed size.", request_id,
        )
    try:
        payload = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return None, _error(
            request, 400, "malformed_json", "Request body must be valid JSON.", request_id,
        )
    if not isinstance(payload, dict):
        return None, _error(
            request, 400, "malformed_json", "Request body must be a JSON object.", request_id,
        )
    return payload, None


def _check_rate_limit(request):
    """Return True when the user/IP has exceeded the per-hour message budget.

    Uses ``cache.add`` + ``cache.incr`` for atomic counting to avoid the
    TOCTOU race inherent in ``cache.get`` → check → ``cache.set``.
    """
    if not request.user.is_authenticated:
        return False
    bucket_key = "job_search_rl:{}:{}:{}".format(
        request.user.pk, request.META.get("REMOTE_ADDR", "?"), _hour_key()
    )
    limit = getattr(settings, "JOB_SEARCH_RATE_LIMIT_PER_HOUR", 120)
    # Initialise the key atomically if it doesn't exist yet.
    cache.add(bucket_key, 0, timeout=3600)
    try:
        used = cache.incr(bucket_key)
    except ValueError:
        # The key expired between add and incr; safe to treat as fresh.
        cache.add(bucket_key, 0, timeout=3600)
        used = cache.incr(bucket_key)
    return used > limit


def _hour_key():
    """Return a coarse key for the current fixed time window."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%d%H")


def _get_active_conversation(user, conversation_id):
    """Return the user's active conversation or None (never another user's)."""
    return JobSearchConversation.objects.filter(
        pk=conversation_id, owner=user, active=True
    ).first()


@login_required
@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def agent_conversation_list(request):
    """Create or resume an authenticated conversation for ``request.user``.

    ``GET``  resumes the most recent active conversation (404 if there is none).
    ``POST`` resumes it by default, or starts a fresh one with
    ``{"create_new": true}``.
    """
    request_id = _request_id(request)

    if request.method == "GET":
        conversation = JobSearchConversation.objects.filter(
            owner=request.user, active=True
        ).first()
        if not conversation:
            return _error(
                request, 404, "no_conversation",
                "No active conversation for this user; POST to create one.", request_id,
            )
        return JsonResponse(
            serialize_conversation(conversation), headers={"X-Request-ID": request_id}
        )

    # POST: resume or create
    payload, error = _body(request, request_id)
    if error:
        return error
    serializer = ConversationCreateSerializer(data=payload)
    if not serializer.is_valid():
        return _error(
            request, 400, "invalid_request",
            "Invalid create/resume request: {}".format(
                _files_to_json(serializer.errors)
            ),
            request_id,
        )
    create_new = serializer.validated_data.get("create_new", False)

    if not create_new:
        conversation = JobSearchConversation.objects.filter(
            owner=request.user, active=True
        ).first()
        if conversation:
            return JsonResponse(
                serialize_conversation(conversation),
                headers={"X-Request-ID": request_id},
            )

    # A fresh conversation is the new resume target: close any prior active one.
    JobSearchConversation.objects.filter(owner=request.user, active=True).update(active=False)
    conversation = JobSearchConversation.objects.create(owner=request.user)
    return JsonResponse(
        serialize_conversation(conversation),
        status=201,
        headers={"X-Request-ID": request_id},
    )


@login_required
@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def agent_conversation_detail(request, conversation_id):
    """Fetch retained history (``GET``) or submit a message (``POST``)."""
    request_id = _request_id(request)
    conversation = _get_active_conversation(request.user, conversation_id)
    if not conversation:
        return _error(
            request, 404, "not_found",
            "Conversation not found or not owned by this user.", request_id,
        )

    if request.method == "GET":
        return JsonResponse(
            serialize_conversation(conversation), headers={"X-Request-ID": request_id}
        )

    payload, error = _body(request, request_id)
    if error:
        return error
    serializer = MessageSubmitSerializer(data=payload)
    if not serializer.is_valid():
        return _error(
            request, 400, "invalid_message",
            "Invalid message: {}".format(_files_to_json(serializer.errors)),
            request_id,
        )
    message_text = serializer.validated_data["content"]
    idempotency_key = serializer.validated_data["idempotency_key"]

    # Idempotent retry: if we already answered this key, replay that answer so
    # a network retry cannot persist a duplicate assistant turn. Rate limit is
    # checked *after* this replay so retries never consume the budget.
    existing_assistant = conversation.messages.filter(
        idempotency_key=idempotency_key, role=JobSearchMessage.Role.ASSISTANT
    ).first()
    if existing_assistant:
        return JsonResponse(
            {
                "message": serialize_message(existing_assistant),
                "preferences_changed": existing_assistant.preferences_changed,
            },
            headers={"X-Request-ID": request_id},
        )

    # Peek at whether the user message already exists for this key before
    # checking the rate limit, so a retry after a transient 500 never
    # exhausts the user's hourly budget.
    existing_user = conversation.messages.filter(
        idempotency_key=idempotency_key, role=JobSearchMessage.Role.USER
    ).first()

    if not existing_user and _check_rate_limit(request):
        return _error(
            request, 429, "rate_limited",
            "Too many messages. Try again shortly.", request_id,
        )

    # Persist the user turn once (even across failed provider calls).
    # ``get_or_create`` is used with a DB-level ``UniqueConstraint`` so
    # concurrent requests with the same key cannot create duplicates.
    if existing_user:
        user_message = existing_user
    else:
        user_message, _user_created = JobSearchMessage.objects.get_or_create(
            conversation=conversation,
            idempotency_key=idempotency_key,
            role=JobSearchMessage.Role.USER,
            defaults={"content": message_text},
        )

    service = JobSearchService()
    try:
        reply_text, changed = service.run_turn(
            conversation=conversation, user_message=user_message.content
        )
    except JobSearchServiceError:
        # User turn remains persisted; the client can retry with same key.
        return _error(
            request, 500, "service_error",
            "We couldn't respond right now. Please retry.", request_id,
        )

    assistant_message, _created = JobSearchMessage.objects.get_or_create(
        conversation=conversation,
        idempotency_key=idempotency_key,
        role=JobSearchMessage.Role.ASSISTANT,
        defaults={
            "content": reply_text,
            "preferences_changed": changed,
        },
    )
    return JsonResponse(
        {
            "message": serialize_message(assistant_message),
            "preferences_changed": changed,
        },
        status=201,
        headers={"X-Request-ID": request_id},
    )


@login_required
@require_GET
def agent_conversation_export(request, conversation_id):
    """Download the user's own conversation history as JSON (no extraneous fields)."""
    request_id = _request_id(request)
    conversation = _get_active_conversation(request.user, conversation_id)
    if not conversation:
        return _error(
            request, 404, "not_found",
            "Conversation not found or not owned by this user.", request_id,
        )
    payload = {
        "user": request.user.username,
        "conversation": serialize_conversation(conversation),
        "profile": serialize_user_profile(conversation),
    }
    filename = "job-search-{}.json".format(conversation.pk)
    response = JsonResponse(payload)
    response["Content-Disposition"] = 'attachment; filename="{}"'.format(filename)
    response["X-Request-ID"] = request_id
    return response


@login_required
@require_POST
def agent_conversation_reset(request, conversation_id):
    """Close the current conversation and start a fresh one (fresh history)."""
    request_id = _request_id(request)
    conversation = _get_active_conversation(request.user, conversation_id)
    if not conversation:
        return _error(
            request, 404, "not_found",
            "Conversation not found or not owned by this user.", request_id,
        )
    conversation.active = False
    conversation.save(update_fields=["active", "modified"])
    new_conversation = JobSearchConversation.objects.create(owner=request.user)
    return JsonResponse(
        serialize_conversation(new_conversation),
        status=201,
        headers={"X-Request-ID": request_id},
    )


@login_required
@require_POST
def agent_conversation_delete(request, conversation_id):
    """Permanently delete the user's conversation and its messages."""
    request_id = _request_id(request)
    conversation = JobSearchConversation.objects.filter(
        pk=conversation_id, owner=request.user
    ).first()
    if not conversation:
        return _error(
            request, 404, "not_found",
            "Conversation not found or not owned by this user.", request_id,
        )
    conversation.messages.all().delete()
    conversation.delete()
    return JsonResponse(
        {"deleted": True}, status=200, headers={"X-Request-ID": request_id}
    )


def _files_to_json(errors):
    """Render DRF serializer errors without leaking internal identifiers."""
    # errors is an OrderedDict; stringify compactly.
    return json.dumps(errors, default=str)