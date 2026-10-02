# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Direct priorities endpoints: read, propose, reset (issue #480).

These sit beside the chat-era ``apply``/``undo`` endpoints in
``crank.views.job_search`` and share its error envelope. They never consult
``INTERACTIVE_AGENT_ENABLED`` or the assistant provider: editing saved
priorities must work while the assistant is unavailable.

* ``GET  /api/agent/preferences/``          - the owner's document, editor
  field metadata and chips. Never creates a row.
* ``POST /api/agent/preferences/propose/``  - read-only proposal for a typed
  patch (the same pipeline chat proposals use). Never writes.
* ``POST /api/agent/preferences/reset/``    - revision-guarded reset with an
  undo token. Chat conversation reset is a different endpoint.
"""
import logging
import uuid

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from crank.agents.job_search.errors import is_database_locked
from crank.services import preferences as pref_services
from crank.views.job_search import _body, _error, _request_id

logger = logging.getLogger(__name__)

_NO_STORE = {"Cache-Control": "private, no-store"}


@login_required
@require_GET
def agent_preference_read(request):
    request_id = _request_id(request)
    snapshot = pref_services.read_for_editor(request.user)
    document = snapshot["preferences"]
    return JsonResponse(
        {
            "exists": snapshot["exists"],
            "revision": snapshot["revision"],
            "schema_version": snapshot["schema_version"],
            "preferences": document,
            "fields": pref_services.editor_fields(document),
            "chips": pref_services.criteria_chips(document),
            "unsupported_criteria": pref_services.unsupported_criteria(document),
        },
        headers={**_NO_STORE, "X-Request-ID": request_id},
    )


@login_required
@require_POST
def agent_preference_propose(request):
    request_id = _request_id(request)
    payload, error = _body(request, request_id)
    if error:
        return error
    patch = payload.get("patch")
    scope = payload.get("scope", "account")
    if not isinstance(patch, dict) or scope not in ("account", "search"):
        return _error(
            request, 400, "invalid_request",
            "A patch object and a valid scope are required.", request_id,
        )
    field_errors = pref_services.patch_field_errors(patch)
    if field_errors:
        return _error(
            request, 400, "invalid_request",
            "Some values are not valid.", request_id,
            extra={"field_errors": field_errors},
        )
    try:
        proposal = pref_services.propose_patch_for_user(
            request.user, patch, scope=scope
        )
    except pref_services.PreferenceError as exc:
        return _error(
            request, 400, "invalid_request", str(exc), request_id,
        )
    return JsonResponse(
        {
            "id": uuid.uuid4().hex,
            "scope": scope,
            "changes": proposal["changes"],
            "change_count": proposal["change_count"],
            "base_revision": proposal["base_revision"],
            "unsupported_criteria": proposal["unsupported_criteria"],
            "currency": proposal["currency"],
            "token": {
                "patch": patch,
                "scope": scope,
                "base_revision": proposal["base_revision"],
                "origin": "direct",
            },
        },
        headers={**_NO_STORE, "X-Request-ID": request_id},
    )


@login_required
@require_POST
def agent_preference_reset(request):
    request_id = _request_id(request)
    payload, error = _body(request, request_id)
    if error:
        return error
    expected_revision = payload.get("expected_revision")
    if (
        expected_revision is None
        or isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        return _error(
            request, 400, "invalid_request",
            "expected_revision must be a non-negative integer.", request_id,
        )
    try:
        result = pref_services.reset(
            request.user, expected_revision=expected_revision
        )
    except pref_services.StalePreferenceError as exc:
        return _error(
            request, 409, "preference_stale",
            "Your priorities changed elsewhere. Review the latest before "
            "resetting.",
            request_id,
            extra={"current_revision": exc.current_revision},
        )
    except Exception as exc:
        if is_database_locked(exc):
            return _error(
                request, 409, "preference_stale",
                "Your priorities could not be reset right now. Please retry.",
                request_id,
            )
        logger.exception("preference reset failed")
        return _error(
            request, 500, "service_error",
            "We couldn't reset your priorities right now. Please retry.",
            request_id,
        )
    return JsonResponse(
        {
            "reset": bool(result["changed"]),
            "revision": result["revision"],
            "changes": result["changes"],
            "undo": result["undo"],
        },
        headers={**_NO_STORE, "X-Request-ID": request_id},
    )
