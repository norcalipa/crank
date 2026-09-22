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


# The deterministic resolver scans the full organization table and normalizes
# in Python, so Unicode/whitespace/case variants share identical semantics on
# every supported database backend. A case-insensitive shortlist cannot be
# authoritative: the database collation does not apply ``normalize_employer_name``
# (NFKC + whitespace collapse + casefold), so it would miss normalization-
# equivalent candidates and turn an ambiguity into a trusted mis-attribution.
#
# Plan deviation (issue #469 review): the approved plan proposed replacing this
# scan with a bounded normalized-name lookup. A persisted normalized-name index
# would require a migration, which AC-15 forbids on this ticket, and a
# collation-based ``iexact`` shortlist is incorrect (above). The complete scan
# is therefore retained as the correctness-preserving choice: the operator
# controlled ``Organization`` table is small relative to ``JobListing``, and
# resolution is O(listings x organizations). This bound is exercised at
# representative cardinality in ``crank/tests/agents/test_employer_resolution.py``
# (``test_exact_name_lookup_scales_to_representative_cardinality``); promotion
# to a persisted normalized identity is tracked for a future ticket that can
# add a migration.
def _organizations_for_exact_name(value: str) -> list[Organization]:
    wanted = normalize_employer_name(value)
    return [
        org
        for org in Organization.objects.all().order_by("pk")
        if normalize_employer_name(org.name) == wanted
    ]


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
            _persist_open_unresolved(listing, result)
    return result


def _persist_open_unresolved(
    listing: JobListing, result: EmployerResolution
) -> UnresolvedEmployer:
    """Maintain exactly one open ``UnresolvedEmployer`` row per listing.

    The caller already holds ``select_for_update`` on the listing row, which
    serializes concurrent resolutions of this listing — the partial unique
    constraint on ``(listing) WHERE resolved=False`` is not emitted on MySQL
    (W036), so the row lock is the real invariant (the
    ``crank.services.scores._persist_locked`` precedent). The whole open set
    is reconciled rather than just the newest row, so duplicates left by an
    earlier race heal back to one open row (the
    ``company_evidence.accept_observation_fields`` heal shape).
    """
    open_rows = list(
        UnresolvedEmployer.objects.filter(listing=listing, resolved=False).order_by("pk")
    )
    defaults = {
        "employer_name": sanitize_employer_text(listing.employer_name),
        "employer_domain": normalize_employer_domain(listing.employer_domain),
        "reason": result.reason,
        "candidates": _bounded_candidates(result),
    }
    if open_rows:
        keep, duplicates = open_rows[0], open_rows[1:]
        for field, value in defaults.items():
            setattr(keep, field, value)
        keep.save(update_fields=[*defaults, "modified"])
        if duplicates:
            now = timezone.now()
            UnresolvedEmployer.objects.filter(
                pk__in=[row.pk for row in duplicates]
            ).update(resolved=True, resolved_at=now)
        return keep
    return UnresolvedEmployer.objects.create(listing=listing, resolved=False, **defaults)


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
