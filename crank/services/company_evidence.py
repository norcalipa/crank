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
    conflicted_attributes: list[str] = []
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
            if observation.status == ObservationStatus.AUTO_APPLIED and any(
                row.extractor_version == CORRECTION_VERSION for row in locked_rows
            ):
                # A staff-accepted manual correction is not overwritten by an
                # unreviewed crawl; only an operator-accepted observation may
                # replace it. A crawl that agrees refreshes the row's
                # freshness; one that disagrees flags the observation
                # CONFLICTED so an operator is told to look.
                manual = next(
                    row for row in locked_rows if row.extractor_version == CORRECTION_VERSION
                )
                if " ".join(value.split()).casefold() == " ".join(
                    manual.value_text.split()
                ).casefold():
                    # Only the check time moves: the crawl fetched a different
                    # page than the user's link, so no fetch or verification is
                    # stamped onto the manual row's provenance.
                    manual.last_checked_at = now
                    manual.save(update_fields=["last_checked_at", "modified"])
                else:
                    conflicted_attributes.append(_OBSERVATION_FIELD_MAP[field_key])
                continue
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
        if conflicted_attributes:
            observation.status = ObservationStatus.CONFLICTED
            observation.conflict_fields = sorted(
                set(observation.conflict_fields) | set(conflicted_attributes)
            )
            observation.save(update_fields=["status", "conflict_fields", "modified"])
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
        if row.extractor_version == CORRECTION_VERSION and not accept_value:
            # Nothing was fetched for a manual correction, so a crawl cannot
            # stamp a fetch or verification onto it.
            return row
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


def resolve_field_evidence_for_orgs(organization_ids):
    """Return accepted evidence per field key per organization in one query.

    The bulk counterpart of :func:`resolve_field_evidence`, used by match
    ranking to avoid an N+1 (issue #467). Only rows in state ``accepted`` are
    returned; when a race left two accepted rows for one field, the newest
    ``observed_at`` wins deterministically.
    """
    if not organization_ids:
        return {}
    rows = CompanyFieldEvidence.objects.filter(
        organization_id__in=organization_ids, state=State.ACCEPTED
    ).order_by("-observed_at", "-id")
    resolved: dict[int, dict[str, CompanyFieldEvidence]] = {}
    for row in rows:
        per_field = resolved.setdefault(row.organization_id, {})
        if row.field_key not in per_field:
            per_field[row.field_key] = row
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


CORRECTION_VERSION = "manual-correction.v1"
CORRECTION_SCOPE_TO_EVIDENCE_SCOPE = {
    "company": None,
    "location": "countries",
    "role": "role_families",
}


class CorrectionNotAcceptable(Exception):
    """Raised when a correction cannot be applied as accepted evidence."""


def scoped_correction_blocked(scope_level: str, scope_value: str, current) -> bool:
    """True when a role/location suggestion could never be accepted for ``current``.

    ``current`` is the field's accepted evidence row (or ``None``). Mirrors the
    refusal in :func:`accept_correction` so the API can say so at submit time.
    """
    scope_key = CORRECTION_SCOPE_TO_EVIDENCE_SCOPE.get(scope_level)
    if not scope_key or current is None:
        return False
    return current.scope_json != {scope_key: [scope_value]}


def accepted_fact_changed(correction, current) -> bool:
    """True when ``current`` is no longer the fact the correction was submitted against."""
    if current is None:
        return correction.current_evidence_id is not None or bool(correction.current_value)
    return current.pk != correction.current_evidence_id or (
        current.value_text != correction.current_value
    )


def accept_correction(
    correction, *, reviewer, now: datetime | None = None, allow_stale: bool = False
):
    """Apply a pending correction as the accepted evidence for its field.

    Same lock-and-supersede shape as :func:`accept_observation_fields`. Team
    scope is refused: matching only understands ``countries`` and
    ``role_families``, so a team scope would silently act company-wide. A
    location or role scope is refused while the field already has an accepted
    fact with a different scope: the model holds one accepted row per field,
    so accepting would turn every listing outside the scope UNKNOWN.
    Nothing was fetched, so ``last_successful_fetch_at`` stays empty.

    Refused (``CorrectionNotAcceptable``) when the company is no longer active,
    when the text carries hidden characters, and, unless ``allow_stale``, when
    the accepted fact changed since the suggestion was submitted. The created
    row gets ``superseded_ids`` and ``overrode_changed_value`` attributes for the audit trail.
    """
    from crank.models.company_correction import find_hidden_character

    if any(
        find_hidden_character(text) is not None
        for text in (correction.proposed_value, correction.scope_value)
    ) or find_hidden_character(correction.evidence_url) is not None:
        raise CorrectionNotAcceptable("the correction contains hidden or control characters")
    scope_key = CORRECTION_SCOPE_TO_EVIDENCE_SCOPE.get(correction.scope_level, False)
    if scope_key is False:
        raise CorrectionNotAcceptable(
            f"{correction.scope_level} scope cannot be applied automatically"
        )
    now = now or timezone.now()
    scope_json = {scope_key: [correction.scope_value]} if scope_key else {}
    with transaction.atomic():
        locked = type(correction).objects.select_for_update().get(pk=correction.pk)
        if locked.status != locked.Status.PENDING:
            raise CorrectionNotAcceptable("correction is no longer pending")
        organization = (
            Organization.objects.select_for_update().filter(pk=locked.organization_id).first()
        )
        if organization is None or organization.status != 1:
            raise CorrectionNotAcceptable("the company is no longer active")
        locked_rows = list(
            CompanyFieldEvidence.objects.filter(
                organization_id=locked.organization_id,
                field_key=locked.field_key,
                state=State.ACCEPTED,
            )
            .select_for_update()
            .order_by("-observed_at", "-id")
        )
        current = locked_rows[0] if locked_rows else None
        fact_changed = accepted_fact_changed(locked, current)
        if not allow_stale and fact_changed:
            raise CorrectionNotAcceptable(
                "the accepted value changed since this was submitted "
                f"(now {current.value_text!r})"
                if current
                else "the accepted fact was removed since this was submitted"
            )
        if scope_json and any(row.scope_json != scope_json for row in locked_rows):
            raise CorrectionNotAcceptable(
                "a scoped correction would replace the fact that already applies to "
                "other listings; the model keeps one accepted fact per field"
            )
        superseded_ids = [row.pk for row in locked_rows]
        if locked_rows:
            CompanyFieldEvidence.objects.filter(pk__in=superseded_ids).update(
                state=State.SUPERSEDED, modified=now
            )
        if current is None or current.value_text != locked.proposed_value:
            last_changed_at = now
        else:
            last_changed_at = current.last_changed_at
        evidence = CompanyFieldEvidence.objects.create(
            organization_id=locked.organization_id,
            field_key=locked.field_key,
            value_text=locked.proposed_value,
            source_url=locked.evidence_url,
            source_domain=(urlsplit(locked.evidence_url).hostname or "").lower(),
            observation=None,
            scope_json=scope_json,
            observed_at=now,
            validation_version=CORRECTION_VERSION,
            extractor_version=CORRECTION_VERSION,
            state=State.ACCEPTED,
            last_checked_at=now,
            last_successful_fetch_at=None,
            last_changed_at=last_changed_at,
            last_verified_at=now,
        )
        locked.status = locked.Status.ACCEPTED
        locked.reviewed_by = reviewer
        locked.reviewed_at = now
        locked.save(update_fields=["status", "reviewed_by", "reviewed_at", "modified"])
        publication.record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=locked.organization_id,
            event_kind=PublicationEvent.EventKind.CHANGED,
            payload={"status": "accepted"},
        )
    correction.status = locked.status
    correction.reviewed_by = reviewer
    correction.reviewed_at = now
    evidence.superseded_ids = superseded_ids
    evidence.overrode_changed_value = fact_changed
    return evidence


def displayed_field_values(organization) -> dict[str, str]:
    """Values the rankings, details header and assistant show per field key.

    These come from the Organization columns, not from evidence rows, so the
    correction form can show both and say which one a correction would change.
    """
    values = {
        "rto_policy": organization.get_rto_policy_display(),
        "funding_round": organization.get_funding_round_display(),
        "accelerated_vesting": "Yes" if organization.accelerated_vesting else "No",
        "company_name": organization.name,
    }
    domain = (urlsplit(organization.url or "").hostname or "").lower()
    if domain:
        values["company_domain"] = domain
    return {key: str(value) for key, value in values.items() if value}


__all__ = [
    "CORRECTION_SCOPE_TO_EVIDENCE_SCOPE",
    "displayed_field_values",
    "CorrectionNotAcceptable",
    "DEFAULT_FRESHNESS_DAYS",
    "FIELD_FRESHNESS_POLICY",
    "EvidenceNotAcceptable",
    "accept_correction",
    "accepted_fact_changed",
    "scoped_correction_blocked",
    "accept_observation_fields",
    "field_evidence_payload",
    "is_stale",
    "observation_field_values",
    "record_check",
    "resolve_field_evidence",
    "resolve_field_evidence_for_orgs",
]
