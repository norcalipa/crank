# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Authenticated API for company suggestions."""

import json

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect

from crank.forms.company_request import CompanyRequestForm
from crank.models.company_request import CompanyRequest


RATE_LIMIT_SECONDS = 60 * 60
DEFAULT_RATE_LIMIT = 5


def _authenticated(request):
    if request.user.is_authenticated:
        return True
    return False


def _request_payload(company_request):
    return {
        "id": company_request.pk,
        "company_name": company_request.company_name,
        "website_url": company_request.website_url,
        "careers_url": company_request.careers_url,
        "reason": company_request.reason,
        "status": company_request.status,
        "created": company_request.created.isoformat(),
        "modified": company_request.modified.isoformat(),
        "duplicate_of": (
            {"id": company_request.duplicate_of_id, "name": company_request.duplicate_of.name}
            if company_request.duplicate_of_id
            else None
        ),
        "approved_organization": (
            {"id": company_request.approved_organization_id,
             "name": company_request.approved_organization.name}
            if company_request.approved_organization_id
            else None
        ),
        "admin_note": company_request.admin_note,
    }


def _error_response(message, status=400, *, field_errors=None):
    payload = {"error": message}
    if field_errors:
        payload["field_errors"] = field_errors
    return JsonResponse(payload, status=status)


def _rate_limited(request):
    limit = getattr(settings, "COMPANY_REQUEST_RATE_LIMIT_PER_HOUR", DEFAULT_RATE_LIMIT)
    key = f"company-request-rate:{request.user.pk}"
    count = cache.get(key)
    if count is None:
        cache.add(key, 1, RATE_LIMIT_SECONDS)
        count = 1
    else:
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, RATE_LIMIT_SECONDS)
            count = 1
    return count > limit


def _duplicate_response(existing_organization, existing_request):
    if existing_organization:
        return _error_response(
            "This company is already in the catalog.",
            status=409,
            field_errors={"company_name": ["An organization with this identity already exists."]},
        )
    return JsonResponse(
        {
            "error": "A pending suggestion for this company already exists.",
            "duplicate_request": {"id": existing_request.pk, "status": existing_request.status},
        },
        status=409,
    )


@csrf_protect
def company_requests(request, pk=None):
    """Create a suggestion or return only the authenticated user's statuses."""
    if not _authenticated(request):
        return _error_response("Sign in to suggest a company.", status=401)

    if request.method == "GET":
        requests = CompanyRequest.objects.filter(requester=request.user).select_related(
            "duplicate_of", "approved_organization"
        )
        if pk is not None:
            request_obj = requests.filter(pk=pk).first()
            if request_obj is None:
                return _error_response("Suggestion not found.", status=404)
            return JsonResponse(_request_payload(request_obj))
        return JsonResponse({"requests": [_request_payload(item) for item in requests]})

    if request.method != "POST" or pk is not None:
        return _error_response("Only POST is supported for new suggestions.", status=405)

    if _rate_limited(request):
        return _error_response(
            "You have reached the suggestion limit. Please try again later.", status=429
        )
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error_response("Send a JSON object.")
    if not isinstance(payload, dict):
        return _error_response("Send a JSON object.")

    form = CompanyRequestForm(payload)
    if not form.is_valid():
        return _error_response("Please correct the highlighted fields.", field_errors=form.errors)
    request_obj = form.save(commit=False)
    request_obj.requester = request.user
    # The form/model normalization makes these keys safe for identity checks.
    request_obj.full_clean()

    existing_organization = CompanyRequest.find_existing_organization(
        normalized_name=request_obj.normalized_name,
        normalized_domain=request_obj.normalized_domain,
    )
    if existing_organization:
        return _duplicate_response(existing_organization, None)
    existing_request = CompanyRequest.objects.filter(status=CompanyRequest.Status.PENDING).filter(
        normalized_name=request_obj.normalized_name
    ).first()
    if existing_request is None:
        existing_request = CompanyRequest.objects.filter(status=CompanyRequest.Status.PENDING).filter(
            normalized_domain=request_obj.normalized_domain
        ).first()
    if existing_request:
        return _duplicate_response(None, existing_request)

    try:
        with transaction.atomic():
            request_obj.save()
    except IntegrityError:
        # A concurrent request may win either partial unique constraint.
        existing_request = CompanyRequest.objects.filter(
            status=CompanyRequest.Status.PENDING
        ).filter(
            normalized_name=request_obj.normalized_name
        ).first() or CompanyRequest.objects.filter(
            status=CompanyRequest.Status.PENDING,
            normalized_domain=request_obj.normalized_domain,
        ).first()
        if existing_request:
            return _duplicate_response(None, existing_request)
        raise
    return JsonResponse(_request_payload(request_obj), status=201)


__all__ = ["company_requests"]
