# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Deterministic resolution of source employer identities."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from crank.models.employer import (
    EmployerAlias,
    UnresolvedEmployer,
    normalize_employer_domain,
    normalize_employer_identifier,
    normalize_employer_name,
    sanitize_employer_text,
)
from crank.models.job import JobListing
from crank.models.organization import Organization

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmployerResolution:
    """The bounded result of one deterministic employer lookup."""

    organization: Organization | None
    reason: str | None
    candidates: tuple[dict[str, int], ...]
    path: str | None

    @property
    def resolved(self) -> bool:
        return self.organization is not None


def _candidate(org: Organization) -> dict[str, int]:
    return {"id": int(org.pk)}


def _organizations_for_alias(kind: str, value: str) -> list[Organization]:
    aliases = EmployerAlias.objects.filter(
        kind=kind,
        value=value,
        status=EmployerAlias.Status.APPROVED,
    ).select_related("organization")
    by_id = {alias.organization_id: alias.organization for alias in aliases}
    return [by_id[key] for key in sorted(by_id)]


def _organizations_for_exact_name(value: str) -> list[Organization]:
    wanted = normalize_employer_name(value)
    # Organization names are operator-controlled but may differ only by case;
    # normalize in Python so Unicode and whitespace have identical semantics on
    # every supported database backend.
    organizations = Organization.objects.all().order_by("pk")
    return [org for org in organizations if normalize_employer_name(org.name) == wanted]


def _resolve_candidates(
    organizations: Iterable[Organization], path: str,
) -> EmployerResolution | None:
    organizations = tuple(organizations)
    candidates = tuple(_candidate(org) for org in organizations)
    if not organizations:
        return None
    if len(organizations) > 1:
        return EmployerResolution(
            organization=None,
            reason=UnresolvedEmployer.Reason.AMBIGUOUS,
            candidates=candidates,
            path=path,
        )
    organization = organizations[0]
    if organization.status != 1:
        reason = UnresolvedEmployer.Reason.INACTIVE
    elif not organization.public:
        reason = UnresolvedEmployer.Reason.NOT_PUBLIC
    else:
        reason = None
    return EmployerResolution(
        organization=organization if reason is None else None,
        reason=reason,
        candidates=candidates,
        path=path,
    )


def _resolution_for_listing(listing: JobListing) -> EmployerResolution:
    external_id = normalize_employer_identifier(
        getattr(listing, "employer_external_id", "")
        or (listing.source_metadata or {}).get("employer_external_id", "")
    )
    domain = normalize_employer_domain(listing.employer_domain)
    name = normalize_employer_name(listing.employer_name)
    levels = (
        (EmployerAlias.AliasKind.EXTERNAL_ID, external_id, "external_id"),
        (EmployerAlias.AliasKind.DOMAIN, domain, "domain"),
        (EmployerAlias.AliasKind.NAME, name, "name"),
    )
    for kind, value, path in levels:
        if value:
            result = _resolve_candidates(_organizations_for_alias(kind, value), path)
            if result is not None:
                return result
    result = _resolve_candidates(_organizations_for_exact_name(name), "exact_name")
    if result is not None:
        return result
    return EmployerResolution(
        organization=None,
        reason=UnresolvedEmployer.Reason.NO_MATCH,
        candidates=(),
        path=None,
    )


def _bounded_candidates(result: EmployerResolution) -> dict[str, list[dict[str, int]]]:
    return {"organization_ids": list(result.candidates)[:32]}


def resolve_employer(
    listing: JobListing,
    *,
    persist: bool = True,
) -> EmployerResolution:
    """Resolve and optionally persist the organization for ``listing``.

    Priority is external ID, normalized domain, reviewed normalized name, then
    exact normalized organization name. No organization is ever created.
    """
    result = _resolution_for_listing(listing)
    logger.info("employer_resolution path=%s reason=%s", result.path, result.reason)
    if not persist:
        return result
    with transaction.atomic():
        listing = JobListing.all_objects.select_for_update().get(pk=listing.pk)
        # Re-evaluate after locking so a concurrent mapping change cannot leave
        # a stale association behind.
        result = _resolution_for_listing(listing)
        if result.organization is not None:
            listing.organization = result.organization
            listing.save(update_fields=["organization", "modified"])
            UnresolvedEmployer.objects.filter(listing=listing, resolved=False).update(
                resolved=True, resolved_at=timezone.now()
            )
        else:
            listing.organization = None
            listing.save(update_fields=["organization", "modified"])
            UnresolvedEmployer.objects.update_or_create(
                listing=listing,
                resolved=False,
                defaults={
                    "employer_name": sanitize_employer_text(listing.employer_name),
                    "employer_domain": normalize_employer_domain(listing.employer_domain),
                    "reason": result.reason,
                    "candidates": _bounded_candidates(result),
                },
            )
    return result


def reprocess_employer_alias(alias: EmployerAlias) -> int:
    """Re-run open unresolved records affected by an approved alias."""
    if alias.status != EmployerAlias.Status.APPROVED:
        return 0
    if alias.kind == EmployerAlias.AliasKind.EXTERNAL_ID:
        value = normalize_employer_identifier(alias.value)
        field = "employer_external_id"
    elif alias.kind == EmployerAlias.AliasKind.DOMAIN:
        value = normalize_employer_domain(alias.value)
        field = "employer_domain"
    else:
        value = normalize_employer_name(alias.value)
        field = "employer_name"
    records = UnresolvedEmployer.objects.filter(resolved=False)
    count = 0
    for record in records.select_related("listing"):
        listing_value = getattr(record.listing, field)
        normalized = (
            normalize_employer_identifier(listing_value)
            if field == "employer_external_id"
            else normalize_employer_domain(listing_value)
            if field == "employer_domain"
            else normalize_employer_name(listing_value)
        )
        if normalized == value:
            result = resolve_employer(record.listing)
            if result.organization is not None:
                count += 1
    return count


__all__ = [
    "EmployerResolution",
    "normalize_employer_domain",
    "normalize_employer_name",
    "normalize_employer_identifier",
    "resolve_employer",
    "reprocess_employer_alias",
    "sanitize_employer_text",
]
