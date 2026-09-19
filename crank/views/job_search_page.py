# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The /chat/ page shell (issue #465).

Public — no login required. Signed-out visitors get a useful assistant
introduction and a validated sign-in call to action instead of being
redirected away; ``crank.auth.visitor_state`` tells a first visit apart from
an expired session so the copy matches. ``@never_cache`` keeps private state
(and the per-request CSRF/session context) out of any shared cache, matching
the algo-page shell's own cache exclusion (``crank/views/index.py``).
"""
from __future__ import annotations

from django.shortcuts import render
from django.views.decorators.cache import never_cache

from crank.auth import FIRST_VISIT_INTRO, SESSION_EXPIRED_MESSAGE, sign_in_url, visitor_state
from crank.models import Organization


def _chat_next_url(request, selected_company_id: int | None) -> str:
    """Return the only non-sensitive chat context allowed in a login handoff."""
    if selected_company_id is None:
        return "/chat/"
    return f"/chat/?company={selected_company_id}"


def _selected_company_id(request) -> int | None:
    """Return the ``?company=<id>`` value only when it names a real org.

    A non-integer or unknown id is ignored — the page still renders
    normally, never a 404 or a redirect.
    """
    raw = request.GET.get("company")
    if raw is None:
        return None
    try:
        company_id = int(raw)
    except (TypeError, ValueError):
        return None
    if not Organization.objects.filter(id=company_id).exists():
        return None
    return company_id


@never_cache
def job_search_page(request):
    """Render the job search assistant page for any requester."""
    selected_company_id = _selected_company_id(request)
    context = {
        "visitor_state": visitor_state(request),
        "sign_in_url": sign_in_url(
            request, next_url=_chat_next_url(request, selected_company_id)
        ),
        "first_visit_intro": FIRST_VISIT_INTRO,
        "session_expired_message": SESSION_EXPIRED_MESSAGE,
        "selected_company_id": selected_company_id,
    }
    return render(request, "crank/job_search.html", context)
