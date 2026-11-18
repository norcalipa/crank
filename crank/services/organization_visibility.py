# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The one statement of which organizations the assistant and job-match
surfaces may show.

An organization is *visible* when it is active (``status == 1``) and public
(``public is True``). This is the same rule employer resolution applies when it
links a listing to an organization, so a listing is never linked to a hidden
employer at ingest; this module covers an organization that is hidden *after*
its listings were linked. The rule is read at request time, so making an
organization visible again brings its rows back with no write.

Keep this interface small: other modules call these three functions and must
not repeat the ``status=1, public=True`` literal.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet

# The rule, stated once.
VISIBLE_STATUS = 1
VISIBLE_FILTER: dict[str, Any] = {"status": VISIBLE_STATUS, "public": True}


def visible_organizations(queryset: QuerySet | None = None) -> QuerySet:
    """Return ``queryset`` (default: all organizations) limited to visible ones."""
    if queryset is None:
        from crank.models.organization import Organization

        queryset = Organization.objects.all()
    return queryset.filter(**VISIBLE_FILTER)


def is_visible_organization(org: Any) -> bool:
    """True when ``org`` is an active, public organization (``None`` is not)."""
    return org is not None and org.status == VISIBLE_STATUS and org.public is True


def visible_employer_q(prefix: str = "") -> Q:
    """``Q`` true when the row's linked organization is absent or visible.

    ``prefix`` is the lookup path to the row that owns the ``organization``
    foreign key, including a trailing ``__`` (for example ``"listing__"`` on
    ``JobMatch``). A row with no organization is not a hidden employer.
    """
    org = f"{prefix}organization"
    return Q(**{f"{org}__isnull": True}) | Q(
        **{f"{org}__{key}": value for key, value in VISIBLE_FILTER.items()}
    )
