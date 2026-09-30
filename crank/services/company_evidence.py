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
- ``record_check`` is the writer of the four freshness timestamps for an
  already-accepted row. Review decisions write them too, but only as part of
  the state change they record: ``record_claim`` / ``observe_review_field``
  set the check timestamps on the open claim they refresh, and
  ``accept_claim`` / ``reject_claim`` stamp ``last_checked_at`` (and
  ``last_verified_at`` / ``last_changed_at`` on accept). In ``record_check``
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

import logging
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from crank.models.company_profile import (
    CompanyFieldEvidence,
    CompanyProfileObservation,
)
from crank.models.monitoring import OperationalChangeAudit
from crank.models.organization import Organization
from crank.models.publication import PublicationEvent
from crank.services import publication

logger = logging.getLogger("crank.services.company_evidence")

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

# Fields a crawl may accept without review: low-consequence, self-describing
# identity facts. Everything that affects eligibility or an employment
# decision waits for staff review (a pending or conflicted claim).
AUTO_APPLY_FIELDS = frozenset(
    {FieldKey.COMPANY_NAME, FieldKey.COMPANY_DOMAIN, FieldKey.LOCATIONS}
)
REVIEW_REQUIRED_FIELDS = frozenset(
    {
        FieldKey.RTO_POLICY,
        FieldKey.FUNDING_ROUND,
        FieldKey.PUBLIC_STATUS,
        FieldKey.ACCELERATED_VESTING,
    }
)

OPEN_CLAIM_STATES = (State.PENDING, State.CONFLICTED)

SCOPE_LIST_KEYS = ("countries", "role_families")
SCOPE_MAX_ITEMS = 10
SCOPE_MAX_ITEM_LENGTH = 100


class EvidenceNotAcceptable(Exception):
    """Raised when an observation cannot produce accepted evidence."""


def strip_unsafe_characters(text: str) -> str:
    """Drop zero-width and bidi format characters; turn control characters into spaces."""
    return "".join(
        " " if unicodedata.category(ch) == "Cc" else ch
        for ch in text
        if unicodedata.category(ch) != "Cf"
    )


def _field_value_text(raw) -> str:
    """Normalize one observation attribute into bounded ``value_text``."""
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        text = ", ".join(str(item) for item in raw if str(item).strip())
    else:
        text = str(raw)
    return " ".join(strip_unsafe_characters(text).split())[:_MAX_VALUE_TEXT]


def field_key_for_observation_attribute(attribute: str) -> str | None:
    """Return the field key an observation attribute backs, if any."""
    for field_key, mapped in _OBSERVATION_FIELD_MAP.items():
        if mapped == attribute:
            return field_key
    return None


def observation_field_values(observation: CompanyProfileObservation) -> dict[str, str]:
    """Return the non-empty field values an observation carries, by key."""
    values: dict[str, str] = {}
    for field_key, attribute in _OBSERVATION_FIELD_MAP.items():
        value = _field_value_text(getattr(observation, attribute, None))
        if value:
            values[field_key] = value
    return values


def accept_observation_fields(
    observation: CompanyProfileObservation,
    *,
    now: datetime | None = None,
    field_keys=None,
) -> list[CompanyFieldEvidence]:
    """Accept the non-empty fields an observation carries as evidence.

    ``field_keys=None`` (the default, and the explicit operator decision)
    accepts every carried field; the crawler passes ``AUTO_APPLY_FIELDS`` so
    only allowlisted identity fields are accepted without review.

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
    if field_keys is not None:
        allowed = set(field_keys)
        values = {key: value for key, value in values.items() if key in allowed}
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
        _lock_organization(organization_id)
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


def _lock_organization(organization_id) -> None:
    Organization.objects.select_for_update().filter(pk=organization_id).first()


def _accepted_row_locked(organization_id, field_key: str):
    return (
        CompanyFieldEvidence.objects.filter(
            organization_id=organization_id, field_key=field_key, state=State.ACCEPTED
        )
        .select_for_update()
        .order_by("-observed_at", "-id")
        .first()
    )


def _close_claims(rows, now: datetime) -> None:
    ids = [row.pk for row in rows]
    if ids:
        CompanyFieldEvidence.objects.filter(pk__in=ids).update(
            state=State.SUPERSEDED, modified=now
        )


def _open_claim_locked(
    organization_id,
    field_key: str,
    value_text: str,
    observation: CompanyProfileObservation,
    state: str,
    now: datetime,
    observed_at: datetime,
) -> CompanyFieldEvidence | None:
    """Open, refresh or skip the claim for one value; caller holds the org lock.

    A claim's value never changes after it is opened: a different value closes
    the old claim (``superseded``) and opens a new row, so a review decision is
    always bound to the value the reviewer saw. A value staff already rejected
    for the same source is not re-queued.
    """
    if CompanyFieldEvidence.objects.filter(
        organization_id=organization_id,
        field_key=field_key,
        source_url=observation.source_url,
        value_text=value_text,
        state=State.REJECTED,
    ).exists():
        return None
    open_rows = list(
        CompanyFieldEvidence.objects.filter(
            organization_id=organization_id,
            field_key=field_key,
            source_url=observation.source_url,
            state__in=OPEN_CLAIM_STATES,
        )
        .select_for_update()
        .order_by("-observed_at", "-id")
    )
    same = next((row for row in open_rows if row.value_text == value_text), None)
    _close_claims([row for row in open_rows if row is not same], now)
    if same is not None:
        same.state = state
        same.observation = observation
        same.observed_at = observed_at
        same.last_checked_at = now
        same.last_successful_fetch_at = now
        same.save(
            update_fields=[
                "state", "observation", "observed_at", "last_checked_at",
                "last_successful_fetch_at", "modified",
            ]
        )
        return same
    claimed_domain = _field_value_text(observation.observed_domain)
    return CompanyFieldEvidence.objects.create(
        organization_id=organization_id,
        field_key=field_key,
        value_text=value_text,
        source_url=observation.source_url,
        source_domain=(urlsplit(observation.source_url).hostname or "").lower(),
        observation=observation,
        scope_json={"claimed_domain": claimed_domain} if claimed_domain else {},
        observed_at=observed_at,
        validation_version=observation.extraction_version,
        extractor_version=observation.extraction_version,
        state=state,
        last_checked_at=now,
        last_successful_fetch_at=now,
        last_changed_at=now,
    )


def record_claim(
    organization,
    field_key: str,
    *,
    value,
    observation: CompanyProfileObservation,
    state: str,
    now: datetime | None = None,
) -> CompanyFieldEvidence | None:
    """Record (or refresh) the open review claim for one observed field.

    A claim is a ``CompanyFieldEvidence`` row in state ``pending`` (nothing
    accepted yet) or ``conflicted`` (the reading differs from the accepted
    value). Claims are never returned by the resolvers, so they can never
    count as verification. At most one open claim exists per
    ``(organization, field_key, source_url)`` and its value is immutable: a
    repeat crawl of the same value refreshes the check timestamps, a new
    value supersedes the old claim and opens a new one. Returns ``None``
    when staff already rejected this value for this source.
    """
    if state not in OPEN_CLAIM_STATES:
        raise ValueError(f"claim state must be one of {OPEN_CLAIM_STATES}, got {state!r}")
    if field_key not in FieldKey.values:
        raise ValueError(f"unknown field key {field_key!r}")
    value_text = _field_value_text(value)
    if not value_text:
        raise ValueError("a claim needs a non-empty value")
    now = now or timezone.now()
    organization_id = getattr(organization, "pk", organization)
    with transaction.atomic():
        _lock_organization(organization_id)
        return _open_claim_locked(
            organization_id, field_key, value_text, observation, state, now,
            observation.observed_at,
        )


def is_staff_reviewed(row: CompanyFieldEvidence) -> bool:
    """True when a person accepted this evidence row.

    Rows from an operator-accepted observation or from an accepted claim are
    staff-reviewed. A row whose observation was ``AUTO_APPLIED`` and has no
    ``claim_accepted`` / ``observation_accepted`` audit is a legacy crawl
    acceptance from before review-required fields existed (#474).
    """
    observation = row.observation
    if observation is not None and observation.status == ObservationStatus.ACCEPTED:
        return True
    return OperationalChangeAudit.objects.filter(
        target_type="company_field_evidence",
        target_id=str(row.pk),
        action__in=("claim_accepted", "observation_accepted"),
        confirmed=True,
    ).exists()


def observe_review_field(
    organization,
    field_key: str,
    *,
    value,
    observation: CompanyProfileObservation,
    conflicted: bool = False,
    reverify: bool = True,
    now: datetime | None = None,
) -> str:
    """Reconcile the open claims for one review-required field with a crawl.

    Runs under the organization lock and returns the outcome:

    * ``"verified"`` - the value equals the accepted row and a person reviewed
      that row, so it is re-verified (``reverify`` permitting);
    * ``"claimed"`` - a claim is open for the value: ``conflicted`` when an
      accepted row holds a different value or ``conflicted`` (the field's own
      attribute conflicted with the prior observation) is set, else
      ``pending``. A legacy, never-reviewed accepted row with the same value
      also gets a ``pending`` claim so staff can review it;
    * ``"none"`` - nothing to claim (the field is absent, or staff rejected
      this value for this source).

    Open claims for this source that the crawl no longer supports (a field
    the page stopped carrying, a value that reverted, or a value equal to a
    reviewed accepted row) are closed as ``superseded``.
    """
    now = now or timezone.now()
    organization_id = getattr(organization, "pk", organization)
    value_text = _field_value_text(value) if value else ""
    with transaction.atomic():
        _lock_organization(organization_id)
        if not value_text:
            _close_claims(
                CompanyFieldEvidence.objects.filter(
                    organization_id=organization_id,
                    field_key=field_key,
                    source_url=observation.source_url,
                    state__in=OPEN_CLAIM_STATES,
                ),
                now,
            )
            return "none"
        accepted = _accepted_row_locked(organization_id, field_key)
        equal = accepted is not None and accepted.value_text == value_text
        if equal and is_staff_reviewed(accepted):
            _close_claims(
                CompanyFieldEvidence.objects.filter(
                    organization_id=organization_id,
                    field_key=field_key,
                    source_url=observation.source_url,
                    state__in=OPEN_CLAIM_STATES,
                ),
                now,
            )
            if not reverify:
                return "none"
            record_check(
                organization, field_key, success=True, verified=True, now=now
            )
            return "verified"
        if accepted is not None and not equal:
            state = State.CONFLICTED
        else:
            state = State.CONFLICTED if conflicted and not equal else State.PENDING
        claim = _open_claim_locked(
            organization_id, field_key, value_text, observation, state, now, now
        )
        return "claimed" if claim is not None else "none"


def validate_claim_scope(scope) -> dict:
    """Return the cleaned scope or raise :class:`EvidenceNotAcceptable`.

    Only ``countries`` and ``role_families`` (at most
    ``SCOPE_MAX_ITEMS`` non-empty strings of at most ``SCOPE_MAX_ITEM_LENGTH``
    characters each) and the crawler's ``claimed_domain`` are understood;
    matching cannot scope by team, so team scope is refused.
    """
    if not isinstance(scope, dict):
        raise EvidenceNotAcceptable("Scope must be a JSON object.")
    if "teams" in scope or "team" in scope:
        raise EvidenceNotAcceptable(
            "Team scope cannot be applied: matching cannot scope by team"
        )
    unknown = sorted(set(scope) - set(SCOPE_LIST_KEYS) - {"claimed_domain"})
    if unknown:
        raise EvidenceNotAcceptable(f"Unsupported scope keys: {', '.join(unknown)}.")
    cleaned: dict = {}
    for key in SCOPE_LIST_KEYS:
        if key not in scope:
            continue
        items = scope[key]
        if not isinstance(items, list) or len(items) > SCOPE_MAX_ITEMS:
            raise EvidenceNotAcceptable(
                f"{key} must be a list of at most {SCOPE_MAX_ITEMS} short non-empty strings."
            )
        entries = set()
        for item in items:
            text = strip_unsafe_characters(item).strip() if isinstance(item, str) else ""
            if not text or len(text) > SCOPE_MAX_ITEM_LENGTH:
                raise EvidenceNotAcceptable(
                    f"{key} must be a list of at most {SCOPE_MAX_ITEMS} short non-empty strings."
                )
            entries.add(text)
        cleaned[key] = sorted(entries)
    if "claimed_domain" in scope:
        domain = scope["claimed_domain"]
        if not isinstance(domain, str) or len(domain) > SCOPE_MAX_ITEM_LENGTH * 3:
            raise EvidenceNotAcceptable("claimed_domain must be a short string.")
        cleaned["claimed_domain"] = strip_unsafe_characters(domain).strip()
    return cleaned


def update_claim_scope(claim: CompanyFieldEvidence, scope, *, reviewer=None) -> dict:
    """Set an open claim's scope under the organization lock.

    Returns ``{"old": ..., "new": ...}``. Only ``scope_json`` and ``modified``
    are written, so a concurrent crawl or decision is never overwritten; a
    claim that is no longer open is refused.
    """
    cleaned = validate_claim_scope(scope)
    with transaction.atomic():
        fresh = _locked_open_claim(claim)
        old = fresh.scope_json
        fresh.scope_json = cleaned
        fresh.save(update_fields=["scope_json", "modified"])
    return {"old": old, "new": cleaned}


def _locked_open_claim(claim: CompanyFieldEvidence) -> CompanyFieldEvidence:
    """Re-read ``claim`` under the organization lock; it must still be open."""
    _lock_organization(claim.organization_id)
    fresh = CompanyFieldEvidence.objects.select_for_update().get(pk=claim.pk)
    if fresh.state not in OPEN_CLAIM_STATES:
        raise EvidenceNotAcceptable(f"claim state {fresh.state!r} is not open for review")
    return fresh


def accept_claim(
    claim: CompanyFieldEvidence, *, reviewer, now: datetime | None = None
) -> CompanyFieldEvidence:
    """Promote an open claim to the accepted evidence for its field.

    Under the organization row lock every accepted row for the field is
    superseded, the claim becomes ``accepted`` with ``last_checked_at`` and
    ``last_verified_at`` set to now, and one publication event is recorded in
    the same transaction so a failure rolls back the whole acceptance.
    ``last_changed_at`` only advances when the value differs from the row
    being replaced. A claim whose backing observation was rejected, or whose
    scope is invalid, is refused. Other open claims for the field are
    re-evaluated: same value closes them, a different value is now
    ``conflicted``.

    The decision is audited here (``claim_accepted``, with the superseded
    evidence ids) in the same transaction, which is also what marks the row
    as staff-reviewed for re-verification (:func:`is_staff_reviewed`).
    """
    now = now or timezone.now()
    with transaction.atomic():
        fresh = _locked_open_claim(claim)
        if (
            fresh.observation_id is not None
            and fresh.observation.status == ObservationStatus.REJECTED
        ):
            raise EvidenceNotAcceptable("the backing observation was rejected")
        validate_claim_scope(fresh.scope_json)
        previous_state = fresh.state
        previous = list(
            CompanyFieldEvidence.objects.filter(
                organization_id=fresh.organization_id,
                field_key=fresh.field_key,
                state=State.ACCEPTED,
            )
            .select_for_update()
            .order_by("-observed_at", "-id")
        )
        _close_claims(previous, now)
        if not previous or previous[0].value_text != fresh.value_text:
            fresh.last_changed_at = now
        else:
            fresh.last_changed_at = previous[0].last_changed_at
        fresh.state = State.ACCEPTED
        fresh.last_checked_at = now
        fresh.last_verified_at = now
        fresh.save()
        others = CompanyFieldEvidence.objects.filter(
            organization_id=fresh.organization_id,
            field_key=fresh.field_key,
            state__in=OPEN_CLAIM_STATES,
        )
        _close_claims(others.filter(value_text=fresh.value_text), now)
        others.filter(state=State.PENDING).exclude(value_text=fresh.value_text).update(
            state=State.CONFLICTED, modified=now
        )
        publication.record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=fresh.organization_id,
            event_kind=PublicationEvent.EventKind.CHANGED,
            payload={"status": State.ACCEPTED.value},
        )
        OperationalChangeAudit.record(
            actor=reviewer,
            target_type="company_field_evidence",
            target_id=fresh.pk,
            action="claim_accepted",
            old_value={"state": previous_state, "value": fresh.value_text},
            new_value={
                "state": State.ACCEPTED.value,
                "value": fresh.value_text,
                "superseded": [row.pk for row in previous],
            },
            confirmed=True,
        )
    logger.info("claim %s accepted by reviewer %s", fresh.pk, getattr(reviewer, "pk", reviewer))
    return fresh


def reject_claim(
    claim: CompanyFieldEvidence, *, reviewer, now: datetime | None = None
) -> CompanyFieldEvidence:
    """Reject an open claim; the row is kept for provenance and never counts."""
    now = now or timezone.now()
    with transaction.atomic():
        fresh = _locked_open_claim(claim)
        previous_state = fresh.state
        fresh.state = State.REJECTED
        fresh.last_checked_at = now
        fresh.save()
        OperationalChangeAudit.record(
            actor=reviewer,
            target_type="company_field_evidence",
            target_id=fresh.pk,
            action="claim_rejected",
            old_value={"state": previous_state, "value": fresh.value_text},
            new_value={"state": State.REJECTED.value, "value": fresh.value_text},
            confirmed=True,
        )
    logger.info("claim %s rejected by reviewer %s", fresh.pk, getattr(reviewer, "pk", reviewer))
    return fresh


def resolve_observation_claims(
    observation: CompanyProfileObservation, state: str, *, now: datetime | None = None
) -> list[int]:
    """Close the open claims of a reviewed observation; returns their ids.

    Accepting an observation whole already wrote its values as accepted
    evidence, so its claims become ``superseded``; rejecting it rejects them.
    """
    if state not in (State.SUPERSEDED, State.REJECTED):
        raise ValueError(f"unsupported claim resolution {state!r}")
    now = now or timezone.now()
    with transaction.atomic():
        if observation.organization_id is not None:
            _lock_organization(observation.organization_id)
        rows = list(
            CompanyFieldEvidence.objects.select_for_update().filter(
                observation=observation, state__in=OPEN_CLAIM_STATES
            )
        )
        ids = [row.pk for row in rows]
        if ids:
            CompanyFieldEvidence.objects.filter(pk__in=ids).update(state=state, modified=now)
    return ids


def scoped_accepted_conflicts(observation: CompanyProfileObservation) -> list[str]:
    """Field keys where accepting ``observation`` whole would replace scoped evidence."""
    if observation.organization_id is None:
        return []
    values = observation_field_values(observation)
    blocked = []
    for row in CompanyFieldEvidence.objects.filter(
        organization_id=observation.organization_id,
        field_key__in=list(values),
        state=State.ACCEPTED,
    ):
        scope = row.scope_json or {}
        if any(scope.get(key) for key in SCOPE_LIST_KEYS) and row.value_text != values[row.field_key]:
            blocked.append(row.field_key)
    return sorted(set(blocked))


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

    Writer of the freshness timestamps for accepted rows. ``last_checked_at``
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


__all__ = [
    "AUTO_APPLY_FIELDS",
    "DEFAULT_FRESHNESS_DAYS",
    "FIELD_FRESHNESS_POLICY",
    "OPEN_CLAIM_STATES",
    "REVIEW_REQUIRED_FIELDS",
    "EvidenceNotAcceptable",
    "accept_claim",
    "accept_observation_fields",
    "field_evidence_payload",
    "field_key_for_observation_attribute",
    "is_stale",
    "observation_field_values",
    "record_check",
    "record_claim",
    "reject_claim",
    "observe_review_field",
    "is_staff_reviewed",
    "resolve_observation_claims",
    "scoped_accepted_conflicts",
    "update_claim_scope",
    "validate_claim_scope",
    "strip_unsafe_characters",
    "resolve_field_evidence",
    "resolve_field_evidence_for_orgs",
]
