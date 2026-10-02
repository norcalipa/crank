# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Authenticated API for proposing corrections to accepted company facts.

Submitting only records a pending proposal: no accepted evidence, organization
or publication row changes until a staff reviewer accepts it, and nothing here
fetches the evidence URL.
"""

import json
import time
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect

from crank.forms.company_correction import CompanyCorrectionForm
from crank.models.company_correction import CompanyCorrection, canonical_text
from crank.models.company_profile import CompanyFieldEvidence
from crank.models.organization import Organization
from crank.services.company_evidence import resolve_field_evidence, scoped_correction_blocked

RATE_LIMIT_SECONDS = 60 * 60
DEFAULT_RATE_LIMIT = 10
DEFAULT_REJECTED_LIMIT = 60
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


def _rate_keys(request):
    base = f"company-correction-rate:{request.user.pk}"
    return base, f"{base}:reset"


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


def _reserve_slot(request):
    """Take one allowance atomically; False (and nothing held) once over the limit."""
    limit = getattr(settings, "COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR", DEFAULT_RATE_LIMIT)
    return _reserve(_rate_keys(request), limit)


def _rejection_keys(request):
    base = f"company-correction-rejected:{request.user.pk}"
    return base, f"{base}:reset"


def _rejections_exhausted(request):
    """The reset key of the exhausted rejection cap, else None."""
    limit = getattr(
        settings, "COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR", DEFAULT_REJECTED_LIMIT
    )
    keys = _rejection_keys(request)
    if (cache.get(keys[0]) or 0) >= limit:
        return keys[1]
    return None


def _count_rejection(request):
    _bump(*_rejection_keys(request))


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


def _too_many(request, reset_key):
    response = _error_response(
        "You have reached the correction limit. Please try again later.", status=429
    )
    response["Retry-After"] = str(_retry_after(request, reset_key))
    return response


def _limited_store(request, payload, idempotency_key):
    """Run the store under an atomically reserved slot.

    The slot is held only by a stored (201) correction; every other outcome
    releases it, and rejected attempts count against a separate, larger cap.
    """
    exhausted = _rejections_exhausted(request)
    if exhausted:
        return _too_many(request, exhausted)
    if not _reserve_slot(request):
        return _too_many(request, _rate_keys(request)[1])
    key = _rate_keys(request)[0]
    try:
        response = _store(request, payload, idempotency_key)
    except BaseException:
        _release(key)
        raise
    if response.status_code != 201:
        _release(key)
        if response.status_code in (400, 404, 409):
            _count_rejection(request)
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
