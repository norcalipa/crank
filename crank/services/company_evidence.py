# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Accepted field-level evidence: acceptance, freshness, and resolution.

This module owns the *decision* layer on top of the raw
``CompanyProfileObservation`` stream. An observation is a point-in-time
reading of a whole page and may be pending, rejected, or conflicted; a
``CompanyFieldEvidence`` row in state ``accepted`` is the claim that one
field, for one organization, is currently backed by an accepted observation,
in a scope, as of four distinct timestamps.

Invariants enforced here:

- Only ``ACCEPTED`` / ``AUTO_APPLIED`` observations with a resolved
  organization can produce accepted evidence (``EvidenceNotAcceptable``
  otherwise).
- Exactly one evidence row per ``(organization, field_key)`` is in state
  ``accepted``. MySQL cannot emit a partial unique constraint (W036), so the
  invariant is serialized with ``select_for_update`` — the same shape as
  ``crank.services.scores._persist_locked``. The *organization* row is
  locked as well, because locking only the evidence rows locks nothing on a
  first accept (there are none yet); and every accepted row for the field is
  superseded, not just the newest, so a duplicate left by any earlier race
  heals back to exactly one on the next accept.
- ``record_check`` is the sole writer of the four freshness timestamps:
  ``last_checked_at`` moves on every attempt, ``last_successful_fetch_at``
  only on a successful fetch, ``last_changed_at`` only when the extracted
  value differs, and ``last_verified_at`` only when the fetch succeeded and
  the value still validates. It never writes a value unless the caller
  passes ``accept_value=True`` to assert the value came from an accepted
  observation, so an unaccepted reading (a conflicted crawl, say) can record
  freshness without ever mutating the accepted claim.
- Provenance (``source_url``/``source_domain``) comes from the host that was
  actually fetched, never from what the crawled page claims about itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from crank.models.company_profile import (
    CompanyFieldEvidence,
    CompanyProfileObservation,
)
from crank.models.organization import Organization
from crank.models.publication import PublicationEvent
from crank.services import publication

FieldKey = CompanyFieldEvidence.FieldKey
State = CompanyFieldEvidence.State
ObservationStatus = CompanyProfileObservation.Status

DEFAULT_FRESHNESS_DAYS = 180

# Maximum accepted age of ``last_verified_at`` per field key, in days. The
# policy is data (not scattered literals) so the API payload, the crawler,
# and any future consumer compute staleness identically. This map enumerates
# every ``FieldKey``, so the ``DEFAULT_FRESHNESS_DAYS`` fallback in
# ``is_stale`` is future-proofing for a key added here later — it is not
# reachable from any registered field key today.
FIELD_FRESHNESS_POLICY: dict[str, int] = {
    FieldKey.RTO_POLICY: 90,
    FieldKey.FUNDING_ROUND: 180,
    FieldKey.PUBLIC_STATUS: 180,
    FieldKey.ACCELERATED_VESTING: 365,
    FieldKey.LOCATIONS: 180,
    FieldKey.COMPANY_NAME: 365,
    FieldKey.COMPANY_DOMAIN: 365,
}

# Observation attributes that can back an accepted field claim. Field keys
# without an observation attribute (``accelerated_vesting``) are accepted
# through other writers; the crawler never invents them.
_OBSERVATION_FIELD_MAP: dict[str, str] = {
    FieldKey.COMPANY_NAME: "observed_name",
    FieldKey.COMPANY_DOMAIN: "observed_domain",
    FieldKey.LOCATIONS: "locations",
    FieldKey.RTO_POLICY: "rto_evidence",
    FieldKey.FUNDING_ROUND: "funding_evidence",
    FieldKey.PUBLIC_STATUS: "public_status_evidence",
}

_MAX_VALUE_TEXT = 500


class EvidenceNotAcceptable(Exception):
    """Raised when an observation cannot produce accepted evidence."""


def _field_value_text(raw) -> str:
    """Normalize one observation attribute into bounded ``value_text``."""
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        text = ", ".join(str(item) for item in raw if str(item).strip())
    else:
        text = str(raw).strip()
    return text[:_MAX_VALUE_TEXT]


def observation_field_values(observation: CompanyProfileObservation) -> dict[str, str]:
    """Return the non-empty field values an observation carries, by key."""
    values: dict[str, str] = {}
    for field_key, attribute in _OBSERVATION_FIELD_MAP.items():
        value = _field_value_text(getattr(observation, attribute, None))
        if value:
            values[field_key] = value
    return values


def accept_observation_fields(
    observation: CompanyProfileObservation, *, now: datetime | None = None
) -> list[CompanyFieldEvidence]:
    """Accept every non-empty field an observation carries as evidence.

    Raises :class:`EvidenceNotAcceptable` unless the observation is in status
    ``ACCEPTED`` or ``AUTO_APPLIED`` and has a resolved organization — a
    pending, rejected, or conflicted observation can never produce an
    accepted evidence row.

    Within one transaction the organization row is locked, every
    ``accepted`` row for each carried field is marked ``superseded``, and one
    replacement row is created. ``last_changed_at`` is set on first accept
    and otherwise only advances when the value actually differs, so an
    unchanged re-acceptance refreshes freshness without claiming the value
    changed. Provenance is taken from the host the crawler actually fetched
    (``source_url``); the page's self-claimed domain is kept in
    ``scope_json['claimed_domain']``, where it reads as a claim rather than
    as attribution. One ``PublicationEvent`` per organization is recorded in
    the same transaction so the provenance cache is invalidated through the
    existing outbox path.
    """
    if observation.organization_id is None:
        raise EvidenceNotAcceptable("observation has no resolved organization")
    if observation.status not in (
        ObservationStatus.ACCEPTED,
        ObservationStatus.AUTO_APPLIED,
    ):
        raise EvidenceNotAcceptable(
            f"observation status {observation.status!r} is not acceptable evidence"
        )
    now = now or timezone.now()
    organization_id = observation.organization_id
    values = observation_field_values(observation)
    fetched_domain = (urlsplit(observation.source_url).hostname or "").lower()
    claimed_domain = _field_value_text(observation.observed_domain)
    scope_json = {"claimed_domain": claimed_domain} if claimed_domain else {}
    created: list[CompanyFieldEvidence] = []
    with transaction.atomic():
        # Lock the organization row, not just its evidence rows: on a first
        # accept there are no evidence rows to lock, so two concurrent
        # accepts would both see no current row and both create one. Locking
        # the parent serializes that case too (locking reads; see
        # crank.services.scores._persist_locked for the precedent).
        Organization.objects.select_for_update().filter(pk=organization_id).first()
        for field_key, value in values.items():
            locked_rows = list(
                CompanyFieldEvidence.objects.filter(
                    organization_id=organization_id,
                    field_key=field_key,
                    state=State.ACCEPTED,
                )
                .select_for_update()
                .order_by("-observed_at", "-id")
            )
            current = locked_rows[0] if locked_rows else None
            if locked_rows:
                # Supersede the whole accepted set, not only ``current``: if
                # an earlier race left duplicates, this accept heals them
                # back to exactly one accepted row.
                CompanyFieldEvidence.objects.filter(
                    pk__in=[row.pk for row in locked_rows]
                ).update(state=State.SUPERSEDED, modified=now)
            if current is None:
                # First accept: the value appears now, so it changed now.
                last_changed_at = now
            elif current.value_text != value:
                last_changed_at = now
            else:
                # Unchanged re-acceptance: no spurious change claim.
                last_changed_at = current.last_changed_at
            created.append(
                CompanyFieldEvidence.objects.create(
                    organization_id=organization_id,
                    field_key=field_key,
                    value_text=value,
                    source_url=observation.source_url,
                    source_domain=fetched_domain,
                    observation=observation,
                    scope_json=dict(scope_json),
                    observed_at=observation.observed_at,
                    validation_version=observation.extraction_version,
                    extractor_version=observation.extraction_version,
                    state=State.ACCEPTED,
                    last_checked_at=now,
                    last_successful_fetch_at=now,
                    last_changed_at=last_changed_at,
                    last_verified_at=now,
                )
            )
        if created:
            publication.record_event(
                target_type=PublicationEvent.TargetType.ORGANIZATION,
                target_id=organization_id,
                event_kind=PublicationEvent.EventKind.OBSERVED,
                payload={
                    "observation_id": observation.pk,
                    "status": observation.status,
                },
            )
    return created


def record_check(
    organization,
    field_key: str,
    *,
    success: bool,
    value=None,
    verified: bool = False,
    accept_value: bool = False,
    now: datetime | None = None,
) -> CompanyFieldEvidence | None:
    """Record one re-check of the accepted claim for ``field_key``.

    The sole writer of the four freshness timestamps. ``last_checked_at``
    advances on every attempt; ``last_successful_fetch_at`` only when
    ``success``; ``last_verified_at`` only when the fetch succeeded *and* the
    value still validates. A failed or rejected fetch therefore advances
    ``last_checked_at`` alone and leaves the other three untouched.

    ``last_changed_at`` (and the stored ``value_text``) move only when the
    caller passes ``accept_value=True`` *and* the value differs. That opt-in
    is the guard on AC-2: a re-check is a freshness signal, and only a caller
    that has already established acceptance may also make it a value signal.
    Without it an unaccepted reading — a conflicted crawl, say — can never be
    smuggled into the accepted row.

    Returns the updated row, or ``None`` when no accepted evidence exists for
    the field (missing evidence is explicit — no row is created here).
    """
    now = now or timezone.now()
    with transaction.atomic():
        row = (
            CompanyFieldEvidence.objects.filter(
                organization=organization,
                field_key=field_key,
                state=State.ACCEPTED,
            )
            .select_for_update()
            .order_by("-observed_at", "-id")
            .first()
        )
        if row is None:
            return None
        update_fields = ["last_checked_at", "modified"]
        row.last_checked_at = now
        if success:
            row.last_successful_fetch_at = now
            update_fields.append("last_successful_fetch_at")
            if accept_value and value is not None:
                value_text = _field_value_text(value)
                if value_text and value_text != row.value_text:
                    row.value_text = value_text
                    row.last_changed_at = now
                    update_fields.extend(["value_text", "last_changed_at"])
            if verified:
                row.last_verified_at = now
                update_fields.append("last_verified_at")
        row.save(update_fields=update_fields)
        return row


def resolve_field_evidence(organization) -> dict[str, CompanyFieldEvidence]:
    """Return the accepted evidence row per field key for an organization.

    Only rows in state ``accepted`` are ever returned, so a pending,
    rejected, or conflicted observation can never surface as verification.
    When a race left two accepted rows for one field, the newest
    ``observed_at`` wins deterministically.
    """
    resolved: dict[str, CompanyFieldEvidence] = {}
    rows = (
        CompanyFieldEvidence.objects.filter(
            organization=organization, state=State.ACCEPTED
        )
        .order_by("-observed_at", "-id")
    )
    for row in rows:
        if row.field_key not in resolved:
            resolved[row.field_key] = row
    return resolved


def is_stale(
    last_verified_at: datetime | None,
    now: datetime | None = None,
    field_key: str | None = None,
) -> bool:
    """True when the claim is unverified or beyond its freshness policy."""
    if last_verified_at is None:
        return True
    now = now or timezone.now()
    policy_days = FIELD_FRESHNESS_POLICY.get(field_key, DEFAULT_FRESHNESS_DAYS)
    return now - last_verified_at > timedelta(days=policy_days)


def field_evidence_payload(organization, *, now: datetime | None = None) -> dict:
    """Build the serialized ``fields`` / ``unverified_fields`` arrays.

    ``stale`` is computed per call from the stored timestamps and the policy
    table (never frozen into a cached flag), and every registered field key
    with no accepted evidence is named in ``unverified_fields`` — missing
    evidence is explicit.
    """
    now = now or timezone.now()
    resolved = resolve_field_evidence(organization)
    fields = []
    for field_key in sorted(resolved):
        row = resolved[field_key]
        fields.append(
            {
                "field_key": row.field_key,
                "state": row.state,
                "value": row.value_text,
                "source_domain": row.source_domain,
                "observed_at": row.observed_at.isoformat(),
                "scope": row.scope_json,
                "last_checked_at": (
                    row.last_checked_at.isoformat() if row.last_checked_at else None
                ),
                "last_successful_fetch_at": (
                    row.last_successful_fetch_at.isoformat()
                    if row.last_successful_fetch_at
                    else None
                ),
                "last_changed_at": (
                    row.last_changed_at.isoformat() if row.last_changed_at else None
                ),
                "last_verified_at": (
                    row.last_verified_at.isoformat() if row.last_verified_at else None
                ),
                "stale": is_stale(row.last_verified_at, now=now, field_key=row.field_key),
            }
        )
    unverified_fields = [key for key in FieldKey.values if key not in resolved]
    return {"fields": fields, "unverified_fields": unverified_fields}


__all__ = [
    "DEFAULT_FRESHNESS_DAYS",
    "FIELD_FRESHNESS_POLICY",
    "EvidenceNotAcceptable",
    "accept_observation_fields",
    "field_evidence_payload",
    "is_stale",
    "observation_field_values",
    "record_check",
    "resolve_field_evidence",
]
