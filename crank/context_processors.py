# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Centralized navigation route definitions for the Crank application shell.

This module is registered as a Django context processor so every template
has access to the canonical list of navigation destinations.  React entry
points can also read these via the data attribute on the <nav> element,
ensuring Django templates and React never drift.
"""
from django.urls import reverse, NoReverseMatch


def _safe_reverse(url_name):
    """Return the URL for *url_name* or '#' if the route is not registered."""
    try:
        return reverse(url_name)
    except NoReverseMatch:
        return "#"


def navigation_context(request):
    """Provide the canonical navigation items to every template."""
    # Determine which section is active based on the request path.
    # Some in-app requests (e.g. admin view test helpers) construct minimal
    # request objects without a ``path`` attribute; fall back to "" so the
    # context processor never crashes template rendering.
    path = getattr(request, "path", "")
    is_admin = path.startswith("/admin") or path.startswith("/staff/")
    is_job_search = path.startswith("/chat/")
    is_help = path.startswith("/help/") or path.startswith("/privacy/")
    is_rankings = not (is_admin or is_job_search or is_help)

    nav_items = [
        {
            "label": "Company Rankings",
            "url": _safe_reverse("index"),
            "icon": "fa-solid fa-ranking-star",
            "id": "nav-rankings",
            "is_active": is_rankings,
            "show_always": True,
        },
        {
            "label": "Job Search",
            "url": _safe_reverse("job_search"),
            "icon": "fa-solid fa-comments",
            "id": "nav-job-search",
            "is_active": is_job_search,
            "show_always": True,
        },
        {
            "label": "Help",
            "url": _safe_reverse("help"),
            "icon": "fa-solid fa-circle-question",
            "id": "nav-help",
            "is_active": is_help,
            "show_always": True,
        },
    ]

    return {
        "nav_items": nav_items,
        "nav_is_admin": is_admin,
        "nav_is_job_search": is_job_search,
        "nav_is_help": is_help,
        "nav_is_rankings": is_rankings,
    }
