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
  not duplicate the persisted user message or machine reply, and at most one
  provider run executes per turn at a time (the MySQL-safe ``JobSearchTurn``
  anchor's unconditional unique constraint plus a row lock serialize
  concurrent same-key submissions; live claims get a stable 409
  ``turn_in_progress``, retries are rate-limited and capped per turn, and
  expired claims are recovered instead of stranding the turn).
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
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import connection, IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
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
from crank.agents.job_search.errors import ConversationClosedError, is_database_locked
from crank.models import JobSearchConversation, JobSearchMessage, JobSearchTurn
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


def _results_json(results) -> str:
    """Serialize structured results for persistence (bounded JSON)."""
    if results is None:
        return ""
    try:
        payload = json.dumps(results.to_json_dict(), separators=(",", ":"))
        # Enforce a size cap to prevent unbounded persistence.
        max_results_bytes = getattr(settings, "JOB_SEARCH_RESULTS_MAX_BYTES", 65536)
        if len(payload.encode("utf-8")) > max_results_bytes:
            logger.warning(
                "results_json exceeds %d bytes, truncating", max_results_bytes
            )
            return ""
        return payload
    except Exception:
        logger.error("failed to serialize results for persistence", exc_info=True)
        return ""


def _run_lifecycle_write(action):
    """Run a lifecycle write, retrying transient backend lock contention.

    Issue #487 review round 2 (MAJOR-2): the guarded turn section claims the
    conversation row's write lock for the whole single-commit window, which is
    milliseconds long. On SQLite the single-writer lock makes a concurrent
    lifecycle write fail fast with ``OperationalError: database is locked``
    (deadlock avoidance) instead of blocking; a short bounded retry then lands
    the reset/delete after the turn commits — the documented "blocks until
    commit" outcome — instead of surfacing a 500 to the user. On locking
    backends (MySQL/PostgreSQL) the write simply blocks on the row lock and no
    retry ever fires.
    """
    last_exc = None
    for attempt in range(6):
        try:
            return action()
        except Exception as exc:
            if not is_database_locked(exc):
                raise
            last_exc = exc
            time.sleep(0.25)
    raise last_exc


def persist_idempotent_message(conversation, idempotency_key, role, defaults):
    """Persist one turn message first-write-wins, duplicate-free.

    Replay guarantee: at most one message per (conversation,
    idempotency_key, role) exists after concurrent retries of the same
    submission. Turns without an idempotency key (legacy clients) are
    outside the guarantee — there is nothing to deduplicate, so every
    submission inserts its own row (the ``unique_jobsearch_message_idempotency``
    condition excludes empty keys, and a get-or-create lookup on an empty
    key would falsely match an earlier legacy turn and swallow the new one).

    On backends that support partial indexes (SQLite, PostgreSQL) the
    conditional ``unique_jobsearch_message_idempotency`` constraint
    enforces first-write-wins. Production MySQL does not create partial
    unique indexes (Django's W036 warnings are expected; see
    ``docs/deployment-migrations.md``), so writers are serialized on the
    parent conversation row with ``select_for_update`` — the same
    MySQL-safe pattern as ``crank/services/scores.py`` ``_persist_locked``.
    The lock is acquired and held INSIDE one atomic block across the
    lookup AND the insert, and is released only at commit: the winner
    commits first; the loser then acquires the lock, finds the winner's
    row, and replays it instead of inserting. (Releasing the lock before
    the write would let two MySQL writers both miss the lookup and both
    insert — MySQL has no conditional unique constraint to catch it.)
    Backends without ``FOR UPDATE`` (SQLite, where the row lock is a no-op
    anyway and a wrapping transaction would only contend SQLite's
    database-wide lock across threads) keep the plain get-or-create:
    there the conditional unique constraint enforces first-write-wins.
    The MySQL two-connection race is proven by
    ``crank/tests/test_mysql_concurrency.py``.
    """
    if not idempotency_key:
        # No key: nothing to deduplicate. Skip the lock and the lookup so
        # every legacy turn inserts its own row.
        return (
            JobSearchMessage.objects.create(
                conversation=conversation, role=role, **defaults
            ),
            True,
        )

    def _get_or_create():
        return JobSearchMessage.objects.get_or_create(
            conversation=conversation,
            idempotency_key=idempotency_key,
            role=role,
            defaults=defaults,
        )

    if connection.features.has_select_for_update:
        with transaction.atomic():
            # Serialize concurrent writers for this conversation (MySQL-safe).
            # The row lock must stay held across the lookup and the insert;
            # it is released only when this block commits.
            JobSearchConversation.objects.select_for_update().get(
                pk=conversation.pk
            )
            return _get_or_create()
    return _get_or_create()


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


def _turn_attempt_cap():
    """Return the bounded per-turn provider attempt cap (issue #458)."""
    return max(1, int(getattr(settings, "JOB_SEARCH_TURN_MAX_ATTEMPTS", 5)))


def _turn_lease_deadline():
    """Return the expiry of a fresh turn claim lease.

    The lease must comfortably exceed the longest legitimate provider run;
    an expired lease means the claiming worker can no longer be running and
    the turn may be recovered (reaped to failed-and-retryable).
    """
    seconds = max(1, int(getattr(settings, "JOB_SEARCH_TURN_LEASE_SECONDS", 300)))
    return timezone.now() + timedelta(seconds=seconds)


def _finalize_turn(turn_id, delivery_state, failure_code=""):
    """Persist a turn's outcome on its anchor row exactly once (issue #458).

    A primary-key ``update`` is used so a conversation deleted mid-flight
    (cascade removes the anchor) makes this a safe no-op instead of raising,
    and the lease is always released.
    """
    JobSearchTurn.objects.filter(pk=turn_id).update(
        delivery_state=delivery_state,
        failure_code=failure_code,
        lease_expires_at=None,
        modified=timezone.now(),
    )


def _release_failed_turn(turn_id, failure_code=""):
    """Best-effort ``failed`` finalize for the retryable 409 outcomes.

    These outcomes (issue #487) can follow backend lock contention that is
    still in progress, so the finalize write itself may hit ``database is
    locked``. Failing the response over it would turn a documented retryable
    409 into a 500; the claim's lease expiry recovers the turn instead
    (``_reap_stale_turns``). Any other backend error still propagates.
    """
    try:
        _finalize_turn(turn_id, JobSearchTurn.DeliveryState.FAILED, failure_code)
    except Exception as exc:
        if not is_database_locked(exc):
            raise
        logger.warning(
            "job_search turn finalize contended; lease expiry will recover it"
        )


def _reap_stale_turns(conversation):
    """Recover turns whose worker lease expired (issue #458).

    A ``pending`` claim past its lease cannot still be running (the lease is
    set above the longest legitimate provider run), so reads and retries mark
    it failed-and-retryable instead of returning ``turn_in_progress`` forever
    after a crashed/interrupted worker.
    """
    JobSearchTurn.objects.filter(
        conversation=conversation,
        delivery_state=JobSearchTurn.DeliveryState.PENDING,
    ).filter(
        Q(lease_expires_at__lt=timezone.now()) | Q(lease_expires_at__isnull=True)
    ).update(
        delivery_state=JobSearchTurn.DeliveryState.FAILED,
        failure_code=JobSearchTurn.FailureCode.WORKER_INTERRUPTED,
        lease_expires_at=None,
        modified=timezone.now(),
    )


def _claim_turn(conversation, turn_key):
    """Claim a turn for a provider run, MySQL-safe (issue #458 CRITICAL).

    Returns ``(turn, outcome, replay_message)`` where ``outcome`` is one of:

    * ``claimed`` — this request owns the claim and must run the provider;
    * ``in_progress`` — another live claim holds the turn (409);
    * ``completed`` — replay the stored assistant reply instead;
    * ``retry_limited`` — the per-turn attempt cap was reached.

    The anchor row's **unconditional** unique constraint guarantees exactly
    one row per ``(conversation, turn_key)`` on every backend — MySQL
    included, where the message-level partial unique constraint is never
    emitted (W036) — so concurrent first submissions serialize on
    ``get_or_create`` and later transitions on ``select_for_update``.
    """
    turn = None
    for _ in range(3):
        try:
            turn, created = JobSearchTurn.objects.get_or_create(
                conversation=conversation,
                turn_key=turn_key,
                defaults={
                    "delivery_state": JobSearchTurn.DeliveryState.PENDING,
                    "attempt_count": 1,
                    "lease_expires_at": _turn_lease_deadline(),
                },
            )
            break
        except IntegrityError:
            # Lost the create race against a winner whose transaction had not
            # committed yet, so get_or_create's internal re-get missed the row.
            # The constraint keeps at most one anchor either way; retry.
            continue
    if turn is None:  # pragma: no cover - defensive; see IntegrityError above
        raise JobSearchTurn.DoesNotExist(
            "turn anchor could not be created for key %s" % turn_key
        )
    if created:
        # Creation *is* the claim: the first request to insert the anchor owns
        # it (attempt 1) and every concurrent loser observes ``pending``.
        return turn, "claimed", None

    with transaction.atomic():
        locked = JobSearchTurn.objects.select_for_update().get(pk=turn.pk)
        if locked.delivery_state == JobSearchTurn.DeliveryState.COMPLETED:
            replay = JobSearchMessage.objects.filter(
                conversation=conversation,
                idempotency_key=turn_key,
                role=JobSearchMessage.Role.ASSISTANT,
            ).first()
            if replay is not None:
                return locked, "completed", replay
            # Completed anchor without a stored reply (deleted out of band);
            # fall through and re-claim so the turn can be answered.
        if (
            locked.delivery_state == JobSearchTurn.DeliveryState.PENDING
            and locked.lease_expires_at is not None
            and locked.lease_expires_at > timezone.now()
        ):
            return locked, "in_progress", None
        if locked.attempt_count >= _turn_attempt_cap():
            if locked.delivery_state == JobSearchTurn.DeliveryState.PENDING:
                # Stale claim at the cap: finalize the interrupted claim; the
                # turn is not retriable anymore either way.
                locked.delivery_state = JobSearchTurn.DeliveryState.FAILED
                locked.failure_code = JobSearchTurn.FailureCode.WORKER_INTERRUPTED
                locked.lease_expires_at = None
                locked.save(update_fields=[
                    "delivery_state", "failure_code", "lease_expires_at", "modified",
                ])
            return locked, "retry_limited", None
        # Failed or stale-pending claim: take over the turn for a new bounded
        # provider attempt (each takeover is a real provider execution).
        locked.delivery_state = JobSearchTurn.DeliveryState.PENDING
        locked.failure_code = ""
        locked.attempt_count += 1
        locked.lease_expires_at = _turn_lease_deadline()
        locked.save(update_fields=[
            "delivery_state", "failure_code", "attempt_count",
            "lease_expires_at", "modified",
        ])
        return locked, "claimed", None


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
        # Recovery-on-read (issue #458): reap turns whose worker lease expired.
        _reap_stale_turns(conversation)
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
    # the write-first conditional UPDATE takes the same conversation-row
    # write lock ``agent_conversation_detail``'s guarded commit uses, so a
    # reset can never interleave between that claim and the assistant-message
    # insert. The bounded retry covers SQLite's single-writer fail-fast when
    # a guarded turn holds the lock (issue #487 review round 2, MAJOR-2).
    def _create_new_conversation():
        with transaction.atomic():
            JobSearchConversation.objects.filter(
                owner=request.user, active=True
            ).update(active=False)
            return JobSearchConversation.objects.create(owner=request.user)

    conversation = _run_lifecycle_write(_create_new_conversation)
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
        # Recovery-on-read (issue #458): turns whose worker lease expired are
        # marked failed-and-retryable before serializing, so an interrupted
        # worker never strands a turn in ``pending`` forever.
        _reap_stale_turns(conversation)
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

    # Idempotent replay: if we already answered this key, replay that answer
    # so a network retry cannot persist a duplicate assistant turn. Replay is
    # a pure read (no provider call, no cost) so it never consumes budget.
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

    # Rate limit every request that can reach the provider — fresh sends AND
    # failed-turn retries alike (issue #458): an existing key is not a free
    # path to unbounded provider calls. Only the completed-turn replay above
    # stays free. The check happens before any persistence, so a throttled
    # request leaves no turn behind.
    if _check_rate_limit(request):
        return _error(
            request, 429, "rate_limited",
            "Too many messages. Try again shortly.", request_id,
        )

    # Turn claim (issue #458): serialize concurrent same-key submissions on
    # the MySQL-safe anchor row (unconditional unique constraint + row lock)
    # so at most one request runs the provider (and re-applies preferences)
    # at a time. Completed turns replay their stored assistant reply; live
    # pending claims 409; failed claims are re-claimed up to the attempt cap;
    # stale pending claims are recovered instead of stranding the turn.
    turn, outcome, replay_message = _claim_turn(conversation, idempotency_key)
    if outcome == "in_progress":
        return _error(
            request, 409, "turn_in_progress",
            "This response is still being generated. Please wait a moment.",
            request_id,
        )
    if outcome == "completed":
        return JsonResponse(
            {
                "message": serialize_message(replay_message),
                "preferences_changed": replay_message.preferences_changed,
            },
            headers={"X-Request-ID": request_id},
        )
    if outcome == "retry_limited":
        return _error(
            request, 429, "retry_limit_reached",
            "This response couldn't be delivered after {} attempts. Please edit "
            "the message and send it again as a new message.".format(
                _turn_attempt_cap()
            ),
            request_id,
        )

    # Persist the user turn once (even across failed provider calls). Only
    # the anchor-claim owner can be here for a fresh key — the anchor race
    # above is what serializes the insert — so a concurrent same-key
    # duplicate user row is impossible even on MySQL, where the message-level
    # partial unique constraint is never emitted.
    user_message, _user_created = JobSearchMessage.objects.get_or_create(
        conversation=conversation,
        idempotency_key=idempotency_key,
        role=JobSearchMessage.Role.USER,
        defaults={"content": message_text},
    )

    # Guarded reply persistence (issue #487 review round 2, MAJOR-1): the
    # hook below is invoked by the orchestrator INSIDE the same database
    # transaction as the lifecycle guard's write-first row claim and the
    # preference-patch write, so the patch and the assistant reply share ONE
    # commit boundary. A reset/delete landing in the old post-patch/pre-reply
    # window can no longer leave a committed patch on a closed conversation:
    # it either blocks on the row lock until the single commit (then closes
    # the conversation) or aborts the whole turn with nothing persisted (409
    # ``conversation_closed``). The hook raises ConversationClosedError for
    # both in-window discard paths; the transport maps it to the 409.
    persisted: dict = {}

    def _persist_reply(*, reply_text, results, preferences_changed):
        try:
            claimed = JobSearchConversation.objects.filter(
                pk=conversation.pk, owner=request.user, active=True
            ).update(active=True)
            if not claimed:
                # Reset or deleted mid-turn: attach nothing.
                raise ConversationClosedError(
                    "conversation was reset or deleted while the assistant "
                    "was responding"
                )
            assistant_message, _created = JobSearchMessage.objects.get_or_create(
                conversation=conversation,
                idempotency_key=idempotency_key,
                role=JobSearchMessage.Role.ASSISTANT,
                defaults={
                    "content": reply_text,
                    "preferences_changed": preferences_changed,
                    "results_json": _results_json(results),
                },
            )
            if not JobSearchConversation.objects.filter(
                pk=conversation.pk, owner=request.user, active=True
            ).exists():
                # Lifecycle state flipped inside the window: discard.
                raise ConversationClosedError(
                    "conversation was reset or deleted while the assistant "
                    "was responding"
                )
            # Mark the turn completed inside the same transaction as the
            # reply (issue #458): a client observing ``completed`` can always
            # find the reply, and a rolled-back commit leaves neither.
            _finalize_turn(turn.pk, JobSearchTurn.DeliveryState.COMPLETED)
        except IntegrityError:
            # The conversation row vanished inside the guarded window
            # (concurrent delete): the FK insert fails and nothing persists.
            raise ConversationClosedError(
                "conversation was reset or deleted while the assistant was "
                "responding"
            )
        persisted["message"] = assistant_message
        persisted["changed"] = preferences_changed

    try:
        service = JobSearchService()
        reply_text, changed, results = service.run_turn(
            conversation=conversation,
            user_message=user_message.content,
            persist_reply=_persist_reply,
        )
    except AssistantUnavailable:
        _finalize_turn(turn.pk, JobSearchTurn.DeliveryState.FAILED, JobSearchTurn.FailureCode.ASSISTANT_UNAVAILABLE)
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
        _finalize_turn(turn.pk, JobSearchTurn.DeliveryState.FAILED, JobSearchTurn.FailureCode.PROVIDER_TIMEOUT)
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
        _finalize_turn(turn.pk, JobSearchTurn.DeliveryState.FAILED, JobSearchTurn.FailureCode.COST_LIMIT)
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
        _finalize_turn(turn.pk, JobSearchTurn.DeliveryState.FAILED, JobSearchTurn.FailureCode.INVALID_OUTPUT)
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
        # No dedicated failure code: the turn is simply failed-and-retryable
        # (the code is internal; clients read only the delivery state).
        _release_failed_turn(turn.pk)
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
        _release_failed_turn(turn.pk)
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
        _release_failed_turn(turn.pk, JobSearchTurn.FailureCode.CONVERSATION_GONE)
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
        _finalize_turn(turn.pk, JobSearchTurn.DeliveryState.FAILED, JobSearchTurn.FailureCode.SERVICE_ERROR)
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
        _finalize_turn(turn.pk, JobSearchTurn.DeliveryState.FAILED, JobSearchTurn.FailureCode.UNEXPECTED_ERROR)
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

    if persisted.get("message") is not None:
        # Guarded hook path: the orchestrator's single transaction already
        # persisted the reply together with any preference patch and the
        # turn's ``completed`` finalize (one commit boundary, issue #487
        # review round 2, MAJOR-1).
        assistant_message = persisted["message"]
        attach_conversation = conversation
    else:
        # Legacy provider without the guarded-persistence hook (demo path):
        # persist here in a self-contained transaction.
        #
        # Late-reply guard, transactional (issues #458 r2 / #487): the
        # conversation may have been reset or deleted while the provider ran.
        # The re-check and the persist happen in ONE transaction, and the
        # re-check is a write-first claim — a conditional ``UPDATE ... WHERE
        # active = 1`` that only matches a conversation that is still owned
        # and active, and that holds the conversation row's write lock (row
        # lock on MySQL/Postgres; SQLite's single-writer serialization) until
        # this transaction commits. Reset/delete take the same lock, so a
        # lifecycle change can never interleave between the claim and the
        # persist: if the reset/delete committed first, the claim matches
        # zero rows and the reply is discarded with a retryable 409
        # ``conversation_closed``; if this transaction claimed first, the
        # reset/delete is applied only after the reply is committed. A flip
        # observed after the insert rolls the insert back (a vanished row
        # surfaces as an FK IntegrityError and is discarded the same way).
        # The pk-based anchor finalize is a safe no-op when the conversation
        # (and with it the anchor) was deleted.

        def _conversation_closed():
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

        results_json_str = _results_json(results)
        try:
            with transaction.atomic():
                attach_conversation = None
                if JobSearchConversation.objects.filter(
                    pk=conversation.pk, owner=request.user, active=True
                ).update(modified=timezone.now()):
                    # Final verification immediately before the persist. The
                    # claim above already excludes out-of-band lifecycle
                    # changes (they block on the row lock until commit); this
                    # read additionally observes a change made through this
                    # transaction's own connection at the exact attach moment.
                    attach_conversation = _reattach_locked_conversation(
                        conversation.pk, request.user
                    )
                if attach_conversation is None:
                    # Nothing was written: the lifecycle change stands and the
                    # anchor is quarantined in the same commit.
                    _finalize_turn(
                        turn.pk,
                        JobSearchTurn.DeliveryState.FAILED,
                        JobSearchTurn.FailureCode.CONVERSATION_GONE,
                    )
                    return _conversation_closed()
                # The assistant reply is persisted before — and atomically
                # with — the turn being marked completed, so any client that
                # ever observes ``completed`` can find the reply (a completed
                # turn always replays it).
                assistant_message, _created = JobSearchMessage.objects.get_or_create(
                    conversation=attach_conversation,
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
                    # Lifecycle state flipped inside the insert window: roll
                    # the insert back with the transaction (issue #487).
                    raise _ReplyDiscarded()
                _finalize_turn(turn.pk, JobSearchTurn.DeliveryState.COMPLETED)
        except (_ReplyDiscarded, IntegrityError):
            # A vanished row surfaces as an FK IntegrityError; both roll the
            # insert back and leave the turn retryable.
            _finalize_turn(
                turn.pk,
                JobSearchTurn.DeliveryState.FAILED,
                JobSearchTurn.FailureCode.CONVERSATION_GONE,
            )
            return _conversation_closed()

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
    assistant_qs = JobSearchMessage.objects.filter(
        conversation=attach_conversation, role=JobSearchMessage.Role.ASSISTANT
    )
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


def _reattach_locked_conversation(conversation_id, user):
    """Re-read the claimed conversation row inside the attach transaction.

    The write-first claim in ``agent_conversation_detail`` matched a
    conversation that was still owned and active, and its row lock excludes
    any out-of-band reset/delete until the attach transaction commits. This
    read still exists so a lifecycle change made through the transaction's
    own connection — the exact TOCTOU window between the claim and the
    persist — is observed before any reply is persisted. It is deliberately
    a module-level seam so tests can interpose a reset or delete at that
    exact moment (issue #458 r2).
    """
    return JobSearchConversation.objects.filter(
        pk=conversation_id, owner=user, active=True
    ).first()


@login_required
@require_POST
def agent_conversation_reset(request, conversation_id):
    """Close the current conversation and start a fresh one (fresh history)."""
    request_id = _request_id(request)
    # Share the late-reply serialization boundary (issue #487): the
    # write-first conditional UPDATE takes the same conversation-row write
    # lock ``agent_conversation_detail``'s guarded commit uses, so a reset
    # either commits before that claim (the reply is discarded with a 409) or
    # lands after the turn's single commit — it can never interleave. The
    # bounded retry covers SQLite's single-writer fail-fast while a guarded
    # turn holds the lock (issue #487 review round 2, MAJOR-2).
    outcome: dict = {"conversation": None}

    def _reset():
        with transaction.atomic():
            claimed = JobSearchConversation.objects.filter(
                pk=conversation_id, owner=request.user, active=True
            ).update(active=False, modified=timezone.now())
            if claimed:
                outcome["conversation"] = JobSearchConversation.objects.create(
                    owner=request.user
                )

    _run_lifecycle_write(_reset)
    if outcome["conversation"] is None:
        return _error(
            request, 404, "not_found",
            "Conversation not found or not owned by this user.", request_id,
        )
    return JsonResponse(
        serialize_conversation(outcome["conversation"]),
        status=201,
        headers={"X-Request-ID": request_id},
    )


@login_required
@require_POST
def agent_conversation_delete(request, conversation_id):
    """Permanently delete the user's conversation and its messages."""
    request_id = _request_id(request)
    # Share the late-reply serialization boundary (issue #487): the
    # write-first DELETE takes the same conversation-row write lock the
    # guarded commit uses; the bounded retry covers SQLite's single-writer
    # fail-fast while a guarded turn holds the lock (issue #487 review round
    # 2, MAJOR-2).
    outcome: dict = {"deleted": False}

    def _delete():
        with transaction.atomic():
            conversation = JobSearchConversation.objects.filter(
                pk=conversation_id, owner=request.user
            ).first()
            if conversation is not None:
                conversation.messages.all().delete()
                conversation.delete()
                outcome["deleted"] = True

    _run_lifecycle_write(_delete)
    if not outcome["deleted"]:
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
