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
* A late reply whose proposed preference patch lost the optimistic-
  concurrency check returns a stable 409 ``preference_stale`` and never
  overwrites the newer preference version; the user turn stays retryable.
* A reply completing after the conversation was reset or deleted mid-turn
  attaches to nothing: a stable 409 ``conversation_closed`` is returned and
  no assistant message is persisted.
* A correlation id is echoed back in ``X-Request-ID`` on every response.
* Message and assistant content is never logged and is always rendered as text.
"""
import json
import logging
import uuid

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crank.agents.job_search import quality
from crank.agents.job_search.demo import (
    AssistantUnavailable,
    JobSearchService,
    JobSearchServiceError,
    ServiceConversationClosed,
    ServiceCostLimit,
    ServiceInvalidOutput,
    ServicePreferenceStale,
    ServicePreferenceVersionUnavailable,
    ServiceTimeout,
)
from crank.models import JobSearchConversation, JobSearchMessage
from crank.services import monitoring
from crank.serializers.job_search import (
    ConversationCreateSerializer,
    MessageSubmitSerializer,
    serialize_conversation,
    serialize_message,
    serialize_user_profile,
)

logger = logging.getLogger("crank.job_search")


class _ReplyDiscarded(Exception):
    """Internal sentinel: the late-reply guard discarded this assistant reply.

    Raised inside the persistence transaction so the whole transaction
    (including any just-inserted assistant message) rolls back; the view maps
    it to the stable 409 ``conversation_closed`` envelope.
    """


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

    # A fresh conversation is the new resume target: close any prior active
    # one under the shared late-reply serialization boundary (issue #487) —
    # the row lock taken here is the same boundary
    # ``agent_conversation_detail``'s persist step uses, so a reset can never
    # interleave between that re-check and the assistant-message insert.
    with transaction.atomic():
        JobSearchConversation.objects.select_for_update().filter(
            owner=request.user, active=True
        ).update(active=False)
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

    try:
        service = JobSearchService()
        reply_text, changed, results = service.run_turn(
            conversation=conversation, user_message=user_message.content
        )
    except AssistantUnavailable:
        monitoring.record_event("interactive_call", {
            "status": "error",
            "reason_code": "assistant_unavailable",
            "correlation_id": request_id,
        })
        # User turn remains persisted; the client can retry with same key.
        return _error(
            request, 503, "assistant_unavailable",
            "The assistant is not available right now. Please try again later.",
            request_id,
        )
    except ServiceTimeout:
        monitoring.record_event("interactive_call", {
            "status": "error",
            "reason_code": "provider_timeout",
            "correlation_id": request_id,
        })
        return _error(
            request, 504, "provider_timeout",
            "The assistant took too long to respond. Please retry.",
            request_id,
        )
    except ServiceCostLimit:
        monitoring.record_event("interactive_call", {
            "status": "error",
            "reason_code": "cost_limit",
            "correlation_id": request_id,
        })
        return _error(
            request, 429, "cost_limit",
            "The assistant has reached its usage limit. "
            "Please try again later.",
            request_id,
        )
    except ServiceInvalidOutput:
        monitoring.record_event("interactive_call", {
            "status": "error",
            "reason_code": "invalid_output",
            "correlation_id": request_id,
        })
        return _error(
            request, 500, "invalid_output",
            "The assistant produced an unexpected response. Please try again.",
            request_id,
        )
    except ServicePreferenceStale:
        monitoring.record_event("interactive_call", {
            "status": "error",
            "reason_code": "preference_stale",
            "correlation_id": request_id,
        })
        # The proposed patch was rejected by the optimistic-concurrency check:
        # the preference row changed while the turn was in flight, so nothing
        # was overwritten. The user turn remains persisted; the client can
        # retry with the same idempotency key (issue #487).
        return _error(
            request, 409, "preference_stale",
            "Your preferences changed while the assistant was responding. "
            "Please retry.",
            request_id,
        )
    except ServicePreferenceVersionUnavailable:
        monitoring.record_event("interactive_call", {
            "status": "error",
            "reason_code": "preference_version_unavailable",
            "correlation_id": request_id,
        })
        # Fail-closed guard (issue #487 review, MAJOR-4): the preference
        # baseline could not be captured at turn start, so a proposed patch
        # was rejected instead of silently applied without the stale check.
        # The user turn remains persisted; the client can retry.
        return _error(
            request, 409, "preference_stale",
            "Your preferences could not be verified while the assistant was "
            "responding. Please retry.",
            request_id,
        )
    except ServiceConversationClosed:
        monitoring.record_event("interactive_call", {
            "status": "error",
            "reason_code": "conversation_closed",
            "correlation_id": request_id,
        })
        # The lifecycle guard aborted a proposed preference patch because the
        # conversation was reset or deleted mid-turn (issue #487 review,
        # MAJOR-2): neither the patch nor the reply persists; the user turn
        # remains retryable with the same idempotency key.
        return _error(
            request, 409, "conversation_closed",
            "This conversation was reset or deleted while the assistant was "
            "responding.",
            request_id,
        )
    except JobSearchServiceError:
        monitoring.record_event("interactive_call", {
            "status": "error",
            "reason_code": "service_error",
            "correlation_id": request_id,
        })
        # User turn remains persisted; the client can retry with same key.
        return _error(
            request, 500, "service_error",
            "We couldn't respond right now. Please retry.", request_id,
        )
    except Exception:
        logger.exception("unexpected job_search error")
        monitoring.record_event("interactive_call", {
            "status": "error",
            "reason_code": "unexpected_error",
            "correlation_id": request_id,
        })
        return _error(
            request, 500, "unexpected_error",
            "An unexpected error occurred. Please try again.",
            request_id,
        )

    # Serialize structured results for persistence (bounded JSON). Pure
    # computation, kept outside the persistence transaction below.
    results_json_str = ""
    if results is not None:
        try:
            import json as _json
            results_json_str = _json.dumps(results.to_json_dict(), separators=(",", ":"))
            # Enforce a size cap to prevent unbounded persistence.
            max_results_bytes = getattr(settings, "JOB_SEARCH_RESULTS_MAX_BYTES", 65536)
            if len(results_json_str.encode("utf-8")) > max_results_bytes:
                logger.warning(
                    "results_json exceeds %d bytes, truncating",
                    max_results_bytes,
                )
                results_json_str = ""
        except Exception:
            logger.error("failed to serialize results for persistence", exc_info=True)
            results_json_str = ""

    # Late-reply lifecycle guard (issue #487): the conversation may have been
    # reset (active=False) or deleted by a concurrent request while the turn
    # was in flight. The re-check and the assistant-message insert are ONE
    # transactional step using an atomic conditional-persistence scheme: a
    # **write-first conditional claim** — ``UPDATE ... WHERE active=True`` —
    # takes the conversation row's write lock on every backend (including
    # SQLite, where FOR UPDATE is a no-op and a read-first transaction fails
    # the SHARED->RESERVED upgrade under concurrent writers). The claim and
    # the reset/delete/create-new endpoints therefore share one serialization
    # boundary: a concurrent reset/delete either commits before the claim —
    # it matches no active row and the reply is discarded — or blocks on the
    # row's write lock until the insert commits; it can never interleave
    # between the two. The in-transaction re-verify below fails closed if the
    # row went inactive inside the window; a vanished row surfaces as an FK
    # IntegrityError — both roll the insert back and map to the stable 409.
    try:
        with transaction.atomic():
            claimed = JobSearchConversation.objects.filter(
                pk=conversation.pk, owner=request.user, active=True
            ).update(active=True)
            if not claimed:
                # Reset or deleted mid-turn: attach nothing.
                raise _ReplyDiscarded()
            assistant_message, _created = JobSearchMessage.objects.get_or_create(
                conversation=conversation,
                idempotency_key=idempotency_key,
                role=JobSearchMessage.Role.ASSISTANT,
                defaults={
                    "content": reply_text,
                    "preferences_changed": changed,
                    "results_json": results_json_str,
                },
            )
            if not JobSearchConversation.objects.filter(
                pk=conversation.pk, owner=request.user, active=True
            ).exists():
                # Lifecycle state flipped inside the window: discard.
                raise _ReplyDiscarded()
    except _ReplyDiscarded:
        monitoring.record_event("interactive_call", {
            "status": "error",
            "reason_code": "conversation_closed",
            "correlation_id": request_id,
        })
        return _error(
            request, 409, "conversation_closed",
            "This conversation was reset or deleted while the assistant was "
            "responding.",
            request_id,
        )
    except IntegrityError:
        # The conversation row vanished (concurrent delete) between the locked
        # re-check and the insert; the FK insert fails and nothing persists.
        logger.info(
            "job_search assistant insert failed: conversation closed mid-persist"
        )
        monitoring.record_event("interactive_call", {
            "status": "error",
            "reason_code": "conversation_closed",
            "correlation_id": request_id,
        })
        return _error(
            request, 409, "conversation_closed",
            "This conversation was reset or deleted while the assistant was "
            "responding.",
            request_id,
        )

    # Helpfulness-gap telemetry (issue #397): size conversations that keep
    # engaging the assistant but never produce a result card. Scalar counts
    # only; no message content or conversation identity is emitted.
    #
    # The one-time emission is made durable and concurrency-safe (issue #423
    # MINOR-2): ``assistant_turns == MIN_HELPFUL_TURNS`` was only a derived
    # snapshot, so two concurrent submissions could both observe the crossing
    # and double-emit (or both observe a later count and miss it). Instead we
    # claim the transition atomically with a conditional update that flips
    # ``helpfulness_gap_emitted`` false -> true; exactly one concurrent
    # request wins the update and emits, the rest see no row to claim.
    assistant_qs = conversation.messages.filter(role=JobSearchMessage.Role.ASSISTANT)
    assistant_turns = assistant_qs.count()
    result_cards = assistant_qs.exclude(results_json="").count()
    if quality.has_helpfulness_gap(
        assistant_turns=assistant_turns, result_cards=result_cards
    ) and JobSearchConversation.objects.filter(
        pk=conversation.pk, helpfulness_gap_emitted=False
    ).update(helpfulness_gap_emitted=True):
        monitoring.record_event(
            "job_search_helpfulness_gap",
            {
                "turns_without_result": assistant_turns - result_cards,
                "empty_result": True,
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
    # Share the late-reply serialization boundary (issue #487): the row lock
    # taken here is the same one ``agent_conversation_detail``'s persist step
    # takes, so a reset can never interleave between that re-check and the
    # assistant-message insert on backends with FOR UPDATE.
    new_conversation = None
    with transaction.atomic():
        conversation = (
            JobSearchConversation.objects.select_for_update()
            .filter(pk=conversation_id, owner=request.user, active=True)
            .first()
        )
        if conversation is not None:
            conversation.active = False
            conversation.save(update_fields=["active", "modified"])
            new_conversation = JobSearchConversation.objects.create(owner=request.user)
    if conversation is None:
        return _error(
            request, 404, "not_found",
            "Conversation not found or not owned by this user.", request_id,
        )
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
    # Share the late-reply serialization boundary (issue #487): the row lock
    # taken here is the same one ``agent_conversation_detail``'s persist step
    # takes, so a delete can never interleave between that re-check and the
    # assistant-message insert on backends with FOR UPDATE.
    deleted = False
    with transaction.atomic():
        conversation = (
            JobSearchConversation.objects.select_for_update()
            .filter(pk=conversation_id, owner=request.user)
            .first()
        )
        if conversation is not None:
            conversation.messages.all().delete()
            conversation.delete()
            deleted = True
    if not deleted:
        return _error(
            request, 404, "not_found",
            "Conversation not found or not owned by this user.", request_id,
        )
    return JsonResponse(
        {"deleted": True}, status=200, headers={"X-Request-ID": request_id}
    )


def _files_to_json(errors):
    """Render DRF serializer errors without leaking internal identifiers."""
    # errors is an OrderedDict; stringify compactly.
    return json.dumps(errors, default=str)
