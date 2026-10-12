# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Authenticated API for proposing corrections to accepted company facts.

Submitting only records a pending proposal: no accepted evidence, organization
or publication row changes until a staff reviewer accepts it, and nothing here
fetches the evidence URL.
"""

import json
import logging
import time
import uuid
from typing import NamedTuple

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect

from crank.client_address import client_address
from crank.forms.company_correction import CompanyCorrectionForm
from crank.models.company_correction import CompanyCorrection, canonical_text
from crank.models.company_profile import CompanyFieldEvidence
from crank.models.organization import Organization
from crank.services.company_evidence import resolve_field_evidence, scoped_correction_blocked

logger = logging.getLogger("crank.company_corrections")

RATE_LIMIT_SECONDS = 60 * 60
DEFAULT_RATE_LIMIT = 10
DEFAULT_REJECTED_LIMIT = 60
DEFAULT_IP_RATE_LIMIT = 20
DEFAULT_IP_REJECTED_LIMIT = 120
# A request in flight is counted in the slice of time it started in, and read
# from that slice and both neighbours, so two requests that start either side
# of a boundary still see each other. Each slice expires on its own, so a
# hold that is never released (a cache error or a stopped worker mid-request)
# is gone within two slices whatever traffic follows.
PENDING_SLICE_SECONDS = 60
STORED = "stored"
REJECTED = "rejected"
SCOPE_ACCOUNT = "account"
SCOPE_NETWORK = "network"
_LIMIT_MESSAGES = {
    SCOPE_ACCOUNT: "You have reached the correction limit. Please try again later.",
    SCOPE_NETWORK: (
        "Too many corrections were sent from your network this hour. "
        "Please try again later."
    ),
}
MAX_ID_DIGITS = 18
MAX_ORGANIZATION_ID = 2**63 - 1
MAX_BODY_BYTES = 8 * 1024
MAX_LIST = 50
_FIELD_LABELS = dict(CompanyFieldEvidence.FieldKey.choices)
_INPUT_KEYS = ("field_key", "proposed_value", "evidence_url", "note")


def _error_response(message, status=400, *, field_errors=None, extra=None):
    payload = {"error": message}
    if field_errors:
        payload["field_errors"] = field_errors
    if extra:
        payload.update(extra)
    return JsonResponse(payload, status=status)


def _private(response):
    response["Cache-Control"] = "private, no-store"
    return response


def _payload(correction):
    return {
        "id": correction.pk,
        "organization": {
            "id": correction.organization_id,
            "name": correction.organization.name,
        },
        "field_key": correction.field_key,
        "field_label": str(_FIELD_LABELS.get(correction.field_key, correction.field_key)),
        "current_value": correction.current_value,
        "proposed_value": correction.proposed_value,
        "evidence_url": correction.evidence_url,
        "scope": {"level": correction.scope_level, "value": correction.scope_value},
        "note": correction.note,
        "status": correction.status,
        "status_label": str(CompanyCorrection.Status(correction.status).label),
        "created": correction.created.isoformat(),
    }


def _retry_after(request, reset_key):
    """Seconds until the caller's hourly window ends (at least 1)."""
    reset_at = cache.get(reset_key)
    if not isinstance(reset_at, (int, float)):
        return RATE_LIMIT_SECONDS
    return max(1, min(RATE_LIMIT_SECONDS, int(reset_at - time.time()) + 1))


def _start_window(reset_key):
    cache.add(reset_key, time.time() + RATE_LIMIT_SECONDS, RATE_LIMIT_SECONDS)


def _bump(key, reset_key):
    """Atomically add one to a counter and return the new value."""
    _start_window(reset_key)
    cache.add(key, 0, RATE_LIMIT_SECONDS)
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, RATE_LIMIT_SECONDS)
        return 1


def _release(key):
    try:
        cache.decr(key)
    except ValueError:
        pass


def _reserve(keys, limit):
    key, reset_key = keys
    if _bump(key, reset_key) > limit:
        _release(key)
        return False
    return True


class _Counter(NamedTuple):
    """One hourly allowance: what it counts, who it binds, and its limit."""

    kind: str
    scope: str
    key: str
    limit: int
    time_slice: int = 0

    @property
    def keys(self):
        return self.key, f"{self.key}:reset"

    @property
    def pending_key(self):
        """Counts requests in flight against a rejected-attempt allowance."""
        return f"{self.key}:pending:{self.time_slice}"

    @property
    def earlier_pending_key(self):
        """The in-flight count of the slice before this request's."""
        return f"{self.key}:pending:{self.time_slice - 1}"

    @property
    def later_pending_key(self):
        """The in-flight count of the slice after this request's."""
        return f"{self.key}:pending:{self.time_slice + 1}"


def _time_slice():
    return int(time.time() // PENDING_SLICE_SECONDS)


def _counters(user_pk, address_key=None):
    """The request's allowances, in the fixed order they are reserved.

    Per-account caps come first within each kind, so the per-address layer can
    only add refusals. The per-address rows exist only for an identified
    client address, and carry its keyed digest, never the address. Every row
    carries the slice of time the request started in, which names where its
    in-flight hold is kept until it settles.
    """
    now = _time_slice()
    rate = getattr(settings, "COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR", DEFAULT_RATE_LIMIT)
    rejected = getattr(
        settings, "COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR", DEFAULT_REJECTED_LIMIT
    )
    counters = [
        _Counter(REJECTED, SCOPE_ACCOUNT, f"company-correction-rejected:{user_pk}", rejected, now)
    ]
    if address_key:
        counters.append(
            _Counter(
                REJECTED,
                SCOPE_NETWORK,
                f"company-correction-ip-rejected:{address_key}",
                getattr(
                    settings,
                    "COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR",
                    DEFAULT_IP_REJECTED_LIMIT,
                ),
                now,
            )
        )
    counters.append(
        _Counter(STORED, SCOPE_ACCOUNT, f"company-correction-rate:{user_pk}", rate, now)
    )
    if address_key:
        counters.append(
            _Counter(
                STORED,
                SCOPE_NETWORK,
                f"company-correction-ip-rate:{address_key}",
                getattr(
                    settings,
                    "COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR",
                    DEFAULT_IP_RATE_LIMIT,
                ),
                now,
            )
        )
    return counters


def _hold_pending(counter):
    """Atomically count one request in flight and return how many there are.

    The hold lives in the request's own slice, whose lifetime is set once,
    when the slice is created, and is never renewed: a hold nobody releases
    stops counting after at most two slices. The count returned includes the
    holds in the slices on either side, read with one cache call, so the cost
    stays three cache calls (add, incr, one read) per allowance.
    """
    key = counter.pending_key
    lifetime = 2 * PENDING_SLICE_SECONDS
    cache.add(key, 0, lifetime)
    try:
        pending = cache.incr(key)
    except ValueError:
        cache.set(key, 1, lifetime)
        pending = 1
    # One read for both neighbours. Each request raises its own slice and then
    # reads the others, so of two requests that start either side of a boundary
    # one always sees the other's hold.
    neighbours = cache.get_many([counter.earlier_pending_key, counter.later_pending_key])
    return pending + sum(
        value for value in neighbours.values() if isinstance(value, int) and value > 0
    )


def _rejections(counter):
    value = cache.get(counter.key)
    return value if isinstance(value, int) else 0


def _take(counter):
    """Reserve one allowance; False, with nothing held, once over the limit.

    A stored allowance is held in its own counter, whose window starts at the
    first request. A rejected-attempt allowance is counted only when an
    attempt is rejected, so its window starts at the first rejection, as it
    did before #538; until the request settles it is held in a separate
    in-flight count, and the two together never exceed the limit. The hold is
    taken before the rejections are read, so two requests cannot both read a
    count that leaves out the other.
    """
    if counter.kind == STORED:
        return _reserve(counter.keys, counter.limit)
    pending = _hold_pending(counter)
    if _rejections(counter) + pending > counter.limit:
        _release(counter.pending_key)
        return False
    return True


def _give_back(counter):
    _release(counter.key if counter.kind == STORED else counter.pending_key)


def _give_back_all(counters):
    """Release every counter, even when the cache fails on one of them."""
    failure = None
    for counter in counters:
        try:
            _give_back(counter)
        except Exception as error:  # the cache backend: keep releasing the rest
            failure = failure or error
    if failure is not None:
        raise failure


def _reserve_all(counters):
    """Take one of every allowance atomically; the refusing counter, else None.

    A refusal releases what was already taken, so it holds and spends nothing,
    and so does a cache error part of the way through.
    """
    held = []
    try:
        for counter in counters:
            if not _take(counter):
                break
            held.append(counter)
        else:
            return None
    except Exception:
        _give_back_all(held)
        raise
    _give_back_all(held)
    return counter


def _settle(counters, status=None):
    """Keep only what the outcome spends and release the rest.

    A stored correction (201) keeps the stored allowances, a rejected attempt
    (400, 404, 409) is counted against the rejected ones, and anything else
    keeps nothing. The stored allowances are released first, as they were
    before #538, and a cache error on one counter does not leave the others
    held.
    """
    kept = STORED if status == 201 else REJECTED if status in (400, 404, 409) else None
    failure = None
    try:
        _give_back_all(
            counter for counter in counters if counter.kind == STORED and kept != STORED
        )
    except Exception as error:  # the cache backend: settle the rest first
        failure = error
    for counter in counters:
        if counter.kind != REJECTED:
            continue
        try:
            try:
                if kept == REJECTED:
                    # Count the rejection before dropping the in-flight hold,
                    # so the sum a concurrent request reads is never short.
                    _bump(*counter.keys)
            finally:
                _give_back(counter)
        except Exception as error:  # the cache backend, as above
            failure = failure or error
    if failure is not None:
        raise failure


def _normalized(value):
    return canonical_text(value)


def _existing_response(existing):
    return _error_response(
        "You already suggested a change to this field.",
        status=409,
        extra={
            "existing": {
                "id": existing.pk,
                "status": existing.status,
                "status_label": str(CompanyCorrection.Status(existing.status).label),
            }
        },
    )


def _list(request):
    queryset = CompanyCorrection.objects.filter(requester=request.user).select_related(
        "organization"
    )
    organization = request.GET.get("organization")
    if organization is not None:
        if not (
            organization.isascii()
            and organization.isdigit()
            and len(organization) <= MAX_ID_DIGITS
        ):
            return _error_response("organization must be an integer id.")
        queryset = queryset.filter(organization_id=int(organization))
    return [_payload(item) for item in queryset[:MAX_LIST]]


def _create(request):
    raw = request.body
    if len(raw) > MAX_BODY_BYTES:
        return _error_response("That request is too large.", status=413)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _error_response("Send a JSON object.")
    if not isinstance(payload, dict):
        return _error_response("Send a JSON object.")

    key_text = payload.get("idempotency_key")
    try:
        idempotency_key = uuid.UUID(key_text) if isinstance(key_text, str) else None
    except ValueError:
        idempotency_key = None
    if idempotency_key is not None:
        replay = (
            CompanyCorrection.objects.filter(
                requester=request.user, idempotency_key=idempotency_key
            )
            .select_related("organization")
            .first()
        )
        if replay is not None:
            return JsonResponse(_payload(replay), status=200)

    return _limited_store(request, payload, idempotency_key)


def _too_many(request, counter):
    response = _error_response(
        _LIMIT_MESSAGES[counter.scope], status=429, extra={"limit_scope": counter.scope}
    )
    if counter.kind == REJECTED and _rejections(counter) < counter.limit:
        # Refused only by requests still in flight: nothing to wait an hour for.
        retry_after = 1
    else:
        retry_after = _retry_after(request, counter.keys[1])
    response["Retry-After"] = str(retry_after)
    return response


def _limited_store(request, payload, idempotency_key):
    """Run the store under atomically reserved allowances.

    Every allowance is reserved before the work, so none can grant more than
    its limit under concurrency. A stored (201) correction holds the stored
    allowances and a rejected attempt is counted against the rejected ones;
    every other outcome, including a refusal, holds nothing. The allowances
    are released on any exit, including a ``BaseException`` such as a worker
    being stopped mid-request, which would otherwise leave them held for the
    rest of the hour.
    """
    address = client_address(request)
    counters = _counters(request.user.pk, address.key if address else None)
    refused = _reserve_all(counters)
    if refused is not None:
        return _too_many(request, refused)
    try:
        response = _store(request, payload, idempotency_key)
    except BaseException:
        _settle(counters)
        raise
    try:
        _settle(counters, response.status_code)
    except Exception:  # the cache backend
        if response.status_code != 201:
            raise
        # The correction is stored and its allowances are kept, as on main; only
        # an in-flight hold failed to release, and it stops counting by itself.
        logger.warning("Releasing an in-flight hold failed after a correction was stored")
    return response


def _store(request, payload, idempotency_key):
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        scope = {}
    data = {
        key: payload[key] if isinstance(payload.get(key), str) else ""
        for key in _INPUT_KEYS
    }
    data["scope_level"] = scope["level"] if isinstance(scope.get("level"), str) else ""
    data["scope_value"] = scope["value"] if isinstance(scope.get("value"), str) else ""
    data["idempotency_key"] = str(idempotency_key) if idempotency_key else ""
    form = CompanyCorrectionForm(data)
    field_errors = {}
    organization_id = payload.get("organization_id")
    if (
        isinstance(organization_id, bool)
        or not isinstance(organization_id, int)
        or not 0 < organization_id <= MAX_ORGANIZATION_ID
    ):
        field_errors["organization_id"] = ["Choose a company."]
    if not form.is_valid():
        field_errors.update({name: list(errors) for name, errors in form.errors.items()})
    if field_errors:
        return _error_response("Please correct the highlighted fields.", field_errors=field_errors)

    correction = form.instance
    correction.requester = request.user
    try:
        with transaction.atomic():
            organization = (
                Organization.objects.select_for_update()
                .filter(pk=organization_id, status=1)
                .first()
            )
            if organization is None:
                return _error_response("Company not found.", status=404)
            existing = CompanyCorrection.objects.filter(
                requester=request.user,
                organization=organization,
                field_key=correction.field_key,
                status=CompanyCorrection.Status.PENDING,
            ).first()
            if existing is not None:
                if existing.idempotency_key == idempotency_key:
                    return JsonResponse(_payload(existing), status=200)
                return _existing_response(existing)
            current = resolve_field_evidence(organization).get(correction.field_key)
            if current is not None and _normalized(current.value_text) == _normalized(
                correction.proposed_value
            ):
                return _error_response(
                    "Please correct the highlighted fields.",
                    field_errors={
                        "proposed_value": ["This is already the accepted value."]
                    },
                )
            if scoped_correction_blocked(
                correction.scope_level, correction.scope_value, current
            ):
                return _error_response(
                    "Please correct the highlighted fields.",
                    field_errors={
                        "scope_level": [
                            "This field already has a company-wide fact, so staff "
                            "can't apply a role- or location-specific change to it. "
                            "Suggest the change for the whole company instead."
                        ]
                    },
                )
            correction.organization = organization
            correction.current_evidence = current
            correction.current_value = current.value_text if current else ""
            correction.save()
    except IntegrityError:
        winner = (
            CompanyCorrection.objects.filter(
                requester=request.user, idempotency_key=idempotency_key
            )
            .select_related("organization")
            .first()
        )
        if winner is None:
            raise
        return JsonResponse(_payload(winner), status=200)
    return JsonResponse(_payload(correction), status=201)


@csrf_protect
def company_corrections(request, pk=None):
    """Create a correction proposal or list only the caller's own."""
    if not request.user.is_authenticated:
        return _error_response("Sign in to suggest a correction.", status=401)

    if request.method == "GET":
        if pk is not None:
            item = (
                CompanyCorrection.objects.filter(requester=request.user, pk=pk)
                .select_related("organization")
                .first()
            )
            if item is None:
                return _private(_error_response("Correction not found.", status=404))
            return _private(JsonResponse(_payload(item)))
        result = _list(request)
        if isinstance(result, JsonResponse):
            return _private(result)
        return _private(JsonResponse({"corrections": result}))

    if request.method != "POST" or pk is not None:
        return _error_response("Only POST is supported for new corrections.", status=405)
    return _create(request)


__all__ = ["company_corrections"]
