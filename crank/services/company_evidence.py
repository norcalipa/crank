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
  the state change they record: ``observe_review_field``
  sets the check timestamps on the open claim they refresh, and
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

import hashlib
import ipaddress
import logging
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Q, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from crank.models.company_request import normalize_public_url
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

# A staff rejection keeps the same value from re-entering the queue from the
# same page for this long; after that the page is asked about again.
REJECTION_SUPPRESSION_DAYS = 30

# ``validation_version`` prefixes of evidence written by a person, not a crawl.
STAFF_VALIDATION_PREFIXES = ("manual-",)

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
    scoped_conflict: str = "raise",
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

    Staff-set narrower scope (``countries`` / ``role_families``) is never
    widened: an unchanged value carries the existing scope onto the
    replacement row, and a different value over scoped evidence raises
    :class:`EvidenceNotAcceptable` (``scoped_conflict="raise"``) or leaves that
    field alone (``"skip"``, the crawler's choice). The check runs under the
    organization lock.
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
    conflicted_attributes: list[str] = []
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
            row_scope = dict(scope_json)
            if current is not None and list_scope(current.scope_json):
                if current.value_text == value:
                    row_scope.update(list_scope(current.scope_json))
                elif scoped_conflict == "skip":
                    continue
                else:
                    raise EvidenceNotAcceptable(
                        f"accepting would replace scoped evidence for {field_key}"
                    )
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
                    scope_json=row_scope,
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


def list_scope(scope) -> dict:
    """The narrowing (countries / role_families) part of a scope, if any."""
    return {
        key: scope[key] for key in SCOPE_LIST_KEYS if scope and key in scope
    }


def value_digest(value_text: str) -> str:
    """Full SHA-256 of a claim value, kept in audits next to the bounded text."""
    return hashlib.sha256(value_text.encode()).hexdigest()


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
    always bound to the value the reviewer saw. A value staff rejected for the
    same source within ``REJECTION_SUPPRESSION_DAYS`` is not re-queued; open
    claims for other values are closed either way, because the page no longer
    states them.
    """
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
    if CompanyFieldEvidence.objects.filter(
        organization_id=organization_id,
        field_key=field_key,
        source_url=observation.source_url,
        value_text=value_text,
        state=State.REJECTED,
        last_checked_at__gt=now - timedelta(days=REJECTION_SUPPRESSION_DAYS),
    ).exists():
        _close_claims(open_rows, now)
        return None
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
    conflicted: bool = False,
    reverify: bool = True,
    now: datetime | None = None,
) -> CompanyFieldEvidence | None:
    """Return the open review claim for one observed field, opening it if needed.

    A thin wrapper over :func:`observe_review_field`, so a caller cannot pick a
    claim state that contradicts the accepted value or the reviewed status.
    Returns ``None`` when the reading opens no claim (the value equals a
    reviewed accepted row, or staff recently rejected it for this source).
    """
    if field_key not in FieldKey.values:
        raise ValueError(f"unknown field key {field_key!r}")
    if not _field_value_text(value):
        raise ValueError("a claim needs a non-empty value")
    observe_review_field(
        organization,
        field_key,
        value=value,
        observation=observation,
        conflicted=conflicted,
        reverify=reverify,
        now=now,
    )
    return CompanyFieldEvidence.objects.filter(
        organization=organization,
        field_key=field_key,
        source_url=observation.source_url,
        value_text=_field_value_text(value),
        state__in=OPEN_CLAIM_STATES,
    ).first()


def staff_reviewed_ids(rows) -> set[int]:
    """Primary keys of ``rows`` a person accepted (one audit query for all).

    Rows from an operator-accepted observation, an accepted claim or a staff
    correction (#477 writes ``validation_version`` ``manual-correction.v1``
    with no observation) are staff-reviewed. A row whose observation was
    ``AUTO_APPLIED`` and has no ``claim_accepted`` / ``observation_accepted``
    audit is a legacy crawl acceptance from before review-required fields
    existed (#474). An ``observation_accepted`` audit stops counting once that
    observation was rejected afterwards; a ``claim_accepted`` audit always does.
    """
    reviewed, undecided = set(), []
    for row in rows:
        observation = row.observation
        if observation is not None and observation.status == ObservationStatus.ACCEPTED:
            reviewed.add(row.pk)
        elif observation is None and (row.validation_version or "").startswith(
            STAFF_VALIDATION_PREFIXES
        ):
            reviewed.add(row.pk)
        else:
            undecided.append(row)
    if undecided:
        by_target = {str(row.pk): row for row in undecided}
        for target_id, action in OperationalChangeAudit.objects.filter(
            target_type="company_field_evidence",
            target_id__in=list(by_target),
            action__in=("claim_accepted", "observation_accepted"),
            confirmed=True,
        ).values_list("target_id", "action"):
            row = by_target[target_id]
            observation_rejected = (
                row.observation is not None
                and row.observation.status == ObservationStatus.REJECTED
            )
            if action == "claim_accepted" or not observation_rejected:
                reviewed.add(row.pk)
    return reviewed


def is_staff_reviewed(row: CompanyFieldEvidence) -> bool:
    """True when a person accepted this evidence row (see :func:`staff_reviewed_ids`)."""
    return row.pk in staff_reviewed_ids([row])


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
        if equal and field_key not in REVIEW_REQUIRED_FIELDS:
            # Identity fields are auto-applied by design: an accepted value this
            # page repeats needs no review, so it opens no (legacy) claim.
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
    claim that is no longer open is refused. A change is audited
    (``scope_change``, unconfirmed: the change form has no confirmation step)
    in the same transaction.
    """
    cleaned = validate_claim_scope(scope)
    with transaction.atomic():
        fresh = _locked_open_claim(claim)
        old = fresh.scope_json
        fresh.scope_json = cleaned
        fresh.save(update_fields=["scope_json", "modified"])
        if old != cleaned:
            OperationalChangeAudit.record(
                actor=reviewer,
                target_type="company_field_evidence",
                target_id=fresh.pk,
                action="scope_change",
                old_value={"scope_json": old},
                new_value={"scope_json": cleaned},
                confirmed=False,
            )
    return {"old": old, "new": cleaned}


def _locked_open_claim(claim: CompanyFieldEvidence) -> CompanyFieldEvidence:
    """Re-read ``claim`` under the organization lock; it must still be open."""
    _lock_organization(claim.organization_id)
    fresh = CompanyFieldEvidence.objects.select_for_update().get(pk=claim.pk)
    if fresh.state not in OPEN_CLAIM_STATES:
        raise EvidenceNotAcceptable(f"claim state {fresh.state!r} is not open for review")
    return fresh


def accepted_scope_for_claim(claim: CompanyFieldEvidence, accepted=None) -> dict:
    """The scope accepting ``claim`` would give the accepted row.

    A claim that says nothing about ``countries`` / ``role_families`` and
    repeats the value of a scoped accepted row inherits that row's narrowing,
    so a later crawl never widens staff-set scope by itself. Staff widen it
    only explicitly, by saving an empty list for the key.
    """
    scope = dict(claim.scope_json or {})
    if accepted is not None and accepted.value_text == claim.value_text:
        for key, items in list_scope(accepted.scope_json).items():
            scope.setdefault(key, items)
    return scope


def restate_open_claims(
    organization_id, field_key: str, value_text: str, now: datetime
) -> tuple[list[int], list[int]]:
    """After ``value_text`` became accepted, re-evaluate the other open claims.

    Same-value claims (other pages) are closed; different-value pending claims
    become ``conflicted``. Returns ``(closed_ids, conflicted_ids)``. The caller
    holds the organization lock.
    """
    others = CompanyFieldEvidence.objects.select_for_update().filter(
        organization_id=organization_id,
        field_key=field_key,
        state__in=OPEN_CLAIM_STATES,
    )
    closed = list(others.filter(value_text=value_text))
    _close_claims(closed, now)
    to_conflict = list(
        others.filter(state=State.PENDING).exclude(value_text=value_text)
    )
    if to_conflict:
        CompanyFieldEvidence.objects.filter(
            pk__in=[row.pk for row in to_conflict]
        ).update(state=State.CONFLICTED, modified=now)
    return [row.pk for row in closed], [row.pk for row in to_conflict]


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
    ``conflicted``. Existing narrower scope is kept for an unchanged value
    (:func:`accepted_scope_for_claim`).

    The decision is audited here (``claim_accepted``, with the superseded
    evidence ids, the claims it closed or turned conflicted, the scope change
    and a full value hash) in the same transaction, which is also what marks
    the row as staff-reviewed for re-verification (:func:`is_staff_reviewed`).
    """
    now = now or timezone.now()
    with transaction.atomic():
        fresh = _locked_open_claim(claim)
        if (
            fresh.observation_id is not None
            and fresh.observation.status == ObservationStatus.REJECTED
        ):
            raise EvidenceNotAcceptable("the backing observation was rejected")
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
        old_scope = fresh.scope_json
        fresh.scope_json = validate_claim_scope(
            accepted_scope_for_claim(fresh, previous[0] if previous else None)
        )
        _close_claims(previous, now)
        if not previous or previous[0].value_text != fresh.value_text:
            fresh.last_changed_at = now
        else:
            fresh.last_changed_at = previous[0].last_changed_at
        fresh.state = State.ACCEPTED
        fresh.last_checked_at = now
        # Verified as of the reading the reviewer accepted, never later: a
        # re-queued legacy reading must not look freshly read.
        fresh.last_verified_at = min(now, fresh.last_successful_fetch_at or now)
        fresh.save()
        closed, conflicted = restate_open_claims(
            fresh.organization_id, fresh.field_key, fresh.value_text, now
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
            old_value={
                "state": previous_state,
                "value": fresh.value_text,
                "value_sha256": value_digest(fresh.value_text),
                "scope_json": old_scope,
            },
            new_value={
                "state": State.ACCEPTED.value,
                "value": fresh.value_text,
                "value_sha256": value_digest(fresh.value_text),
                "value_length": len(fresh.value_text),
                "scope_json": fresh.scope_json,
                "superseded": [row.pk for row in previous],
                "claims_closed": closed,
                "claims_conflicted": conflicted,
            },
            confirmed=True,
        )
    logger.info("claim %s accepted by reviewer %s", fresh.pk, getattr(reviewer, "pk", reviewer))
    return fresh


def _restate_after_retraction(
    organization_id, field_key: str, value_text: str, now: datetime, *, exclude_pk
) -> dict:
    """After staff retracted ``value_text``, reconcile the field's other open claims.

    Same-value claims from other pages are closed (the value was just rejected);
    conflicted claims have no accepted row to conflict with any more, so they
    become pending. The caller holds the organization lock.
    """
    others = CompanyFieldEvidence.objects.select_for_update().filter(
        organization_id=organization_id,
        field_key=field_key,
        state__in=OPEN_CLAIM_STATES,
    ).exclude(pk=exclude_pk)
    closed = list(others.filter(value_text=value_text))
    _close_claims(closed, now)
    reopened = list(others.filter(state=State.CONFLICTED).exclude(value_text=value_text))
    if reopened:
        CompanyFieldEvidence.objects.filter(
            pk__in=[row.pk for row in reopened]
        ).update(state=State.PENDING, modified=now)
    return {"closed": [r.pk for r in closed], "pending": [r.pk for r in reopened]}


def reject_claim(
    claim: CompanyFieldEvidence, *, reviewer, now: datetime | None = None
) -> CompanyFieldEvidence:
    """Reject an open claim; the row is kept for provenance and never counts.

    A claim that repeats the value of the currently accepted row when no
    person ever reviewed that row (a legacy crawl acceptance) is a rejection of
    that fact: the row is marked ``superseded`` in the same transaction, so the
    field becomes unverified and stops driving matching. The audit lists it as
    ``retracted``. A reviewed accepted row is never touched by a rejection.
    """
    now = now or timezone.now()
    with transaction.atomic():
        fresh = _locked_open_claim(claim)
        previous_state = fresh.state
        retracted = []
        accepted = _accepted_row_locked(fresh.organization_id, fresh.field_key)
        reconciled = {"closed": [], "pending": []}
        if (
            fresh.field_key in REVIEW_REQUIRED_FIELDS
            and accepted is not None
            and accepted.value_text == fresh.value_text
            and not is_staff_reviewed(accepted)
        ):
            _close_claims([accepted], now)
            retracted = [accepted.pk]
            reconciled = _restate_after_retraction(
                fresh.organization_id, fresh.field_key, fresh.value_text, now,
                exclude_pk=fresh.pk,
            )
        publication.record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=fresh.organization_id,
            event_kind=PublicationEvent.EventKind.CHANGED,
            payload={"status": "retracted" if retracted else "rejected"},
        )
        fresh.state = State.REJECTED
        fresh.last_checked_at = now
        fresh.save()
        OperationalChangeAudit.record(
            actor=reviewer,
            target_type="company_field_evidence",
            target_id=fresh.pk,
            action="claim_rejected",
            old_value={
                "state": previous_state,
                "value": fresh.value_text,
                "value_sha256": value_digest(fresh.value_text),
            },
            new_value={
                "state": State.REJECTED.value,
                "value": fresh.value_text,
                "value_sha256": value_digest(fresh.value_text),
                "value_length": len(fresh.value_text),
                "retracted": retracted,
                "claims_closed": reconciled["closed"],
                "claims_pending": reconciled["pending"],
            },
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
    """Field keys where accepting ``observation`` whole would replace scoped evidence.

    Only a *different* value blocks: an unchanged value carries the existing
    scope forward (:func:`accept_observation_fields`).
    """
    if observation.organization_id is None:
        return []
    values = observation_field_values(observation)
    blocked = []
    for row in CompanyFieldEvidence.objects.filter(
        organization_id=observation.organization_id,
        field_key__in=list(values),
        state=State.ACCEPTED,
    ):
        if list_scope(row.scope_json) and row.value_text != values[row.field_key]:
            blocked.append(row.field_key)
    return sorted(set(blocked))


def rejected_value_conflicts(observation: CompanyProfileObservation) -> list[str]:
    """Review-required field keys whose carried value staff recently rejected."""
    if observation.organization_id is None:
        return []
    values = observation_field_values(observation)
    since = timezone.now() - timedelta(days=REJECTION_SUPPRESSION_DAYS)
    blocked = set()
    for field_key, value_text in values.items():
        if field_key not in REVIEW_REQUIRED_FIELDS:
            continue
        if CompanyFieldEvidence.objects.filter(
            organization_id=observation.organization_id,
            field_key=field_key,
            value_text=value_text,
            state=State.REJECTED,
            last_checked_at__gt=since,
        ).exists():
            blocked.add(field_key)
    return sorted(blocked)


def legacy_unreviewed_rows(organization=None) -> list[CompanyFieldEvidence]:
    """Accepted review-required rows no person ever reviewed (pre-#474 crawls).

    These stay in effect until staff decide; the report is the only way to find
    the ones whose organization is never recrawled.
    """
    rows = CompanyFieldEvidence.objects.filter(
        state=State.ACCEPTED, field_key__in=sorted(REVIEW_REQUIRED_FIELDS)
    ).select_related("organization", "observation")
    if organization is not None:
        rows = rows.filter(organization=organization)
    rows = list(rows.order_by("organization_id", "field_key", "id"))
    reviewed = staff_reviewed_ids(rows)
    return [row for row in rows if row.pk not in reviewed]


def queue_legacy_claim(row: CompanyFieldEvidence, *, now: datetime | None = None):
    """Open a ``pending`` claim re-stating a legacy accepted row, for review.

    Lets staff review an organization that no crawl will revisit. The claim
    repeats the row's value and source, so accepting it makes the value
    reviewed (scope carries over) and rejecting it retracts the legacy row.
    Returns the claim, or ``None`` when the row is not a legacy unreviewed
    accepted row or the page already has an open claim for the field.
    """
    now = now or timezone.now()
    with transaction.atomic():
        _lock_organization(row.organization_id)
        fresh = CompanyFieldEvidence.objects.select_for_update().get(pk=row.pk)
        if fresh.state != State.ACCEPTED or is_staff_reviewed(fresh):
            return None
        if CompanyFieldEvidence.objects.filter(
            organization_id=fresh.organization_id,
            field_key=fresh.field_key,
            source_url=fresh.source_url,
            state__in=OPEN_CLAIM_STATES,
        ).exists():
            # One open claim per (organization, field, page): a page that
            # already has one (for this value or another) is not re-stated.
            return None
        publication.record_event(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=fresh.organization_id,
            event_kind=PublicationEvent.EventKind.CHANGED,
            payload={"status": "claim_queued"},
        )
        return CompanyFieldEvidence.objects.create(
            organization_id=fresh.organization_id,
            field_key=fresh.field_key,
            value_text=fresh.value_text,
            source_url=fresh.source_url,
            source_domain=fresh.source_domain,
            observation=fresh.observation,
            scope_json=(
                {"claimed_domain": fresh.scope_json["claimed_domain"]}
                if (fresh.scope_json or {}).get("claimed_domain")
                else {}
            ),
            observed_at=fresh.observed_at,
            validation_version=fresh.validation_version,
            extractor_version=fresh.extractor_version,
            state=State.PENDING,
            last_checked_at=now,
            last_successful_fetch_at=fresh.last_successful_fetch_at,
            last_changed_at=fresh.last_changed_at,
        )


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


def resolve_field_evidence_for_orgs(organization_ids, *, summary_only: bool = False):
    """Return accepted evidence per field key per organization in one query.

    The bulk counterpart of :func:`resolve_field_evidence`, used by match
    ranking to avoid an N+1 (issue #467). Only rows in state ``accepted`` are
    returned; when a race left two accepted rows for one field, the newest
    ``observed_at`` wins deterministically. ``summary_only`` defers the text
    columns for callers that only count (the rankings summaries).
    """
    if not organization_ids:
        return {}
    rows = CompanyFieldEvidence.objects.filter(
        organization_id__in=organization_ids, state=State.ACCEPTED
    ).order_by("-observed_at", "-id")
    if summary_only:
        rows = rows.only(
            "organization_id", "field_key", "observed_at", "last_verified_at", "scope_json"
        )
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


TRACKED_FIELD_COUNT = len(FieldKey.values)
EVIDENCE_SCHEMA_VERSION = 3

# The public provenance payload lists only the newest few open claims: claims
# on abandoned crawl URLs pile up, and the payload is served to everyone.
PENDING_REVIEW_PER_FIELD = 2
PENDING_REVIEW_MAX = 10


def field_status(row: CompanyFieldEvidence, *, now: datetime | None = None) -> str:
    """``verified`` (accepted, within policy) or ``stale`` for an accepted row."""
    if is_stale(row.last_verified_at, now=now, field_key=row.field_key):
        return "stale"
    return "verified"


def safe_source_url(row: CompanyFieldEvidence) -> str | None:
    """The row's source URL only when it is a public HTTPS URL on its own domain.

    A link is shown only for a validated destination: the URL must pass
    ``normalize_evidence_url`` (#477's check: public HTTPS, no IP literal, no
    mixed-script look-alike host) on an ASCII, non-punycode domain and its host must equal the ASCII
    ``source_domain`` or be a subdomain of it, so a stored URL can never send
    the reader somewhere other than the domain the dialog names.
    """
    from crank.models.company_correction import normalize_evidence_url

    domain = (row.source_domain or "").strip().rstrip(".").casefold()
    if not domain or not row.source_url:
        return None
    if not (domain.isascii() and row.source_url.isascii()):
        return None
    if any(label.startswith("xn--") for label in domain.split(".")):
        return None
    try:
        url = normalize_evidence_url(row.source_url)
    except ValidationError:
        return None
    host = (urlsplit(url).hostname or "").casefold()
    if host == domain or host.endswith("." + domain):
        return url
    return None


def _review_state(conflicted: int) -> str:
    """The one escalation rule: any conflicted claim makes the field conflicted."""
    return "conflicted" if conflicted else "pending"


def _open_claim_aggregate(organization_ids):
    """Per (organization, field): total and conflicted open-claim counts."""
    return (
        CompanyFieldEvidence.objects.filter(
            organization_id__in=organization_ids, state__in=OPEN_CLAIM_STATES
        )
        .values("organization_id", "field_key")
        .annotate(
            total=Count("id"),
            conflicted=Count("id", filter=Q(state=State.CONFLICTED)),
        )
    )


def _capped_open_claims(organization_id) -> list[CompanyFieldEvidence]:
    """The newest open claims, at most N per field and M overall, in one query."""
    ranked = CompanyFieldEvidence.objects.filter(
        organization_id=organization_id, state__in=OPEN_CLAIM_STATES
    ).annotate(
        field_rank=Window(
            RowNumber(),
            partition_by=F("field_key"),
            order_by=[F("observed_at").desc(), F("id").desc()],
        )
    )
    return list(
        ranked.filter(field_rank__lte=PENDING_REVIEW_PER_FIELD).order_by(
            "-observed_at", "-id"
        )[:PENDING_REVIEW_MAX]
    )


def open_claims_by_org(organization_ids) -> dict[int, dict[str, str]]:
    """``{org_id: {field_key: "pending"|"conflicted"}}`` in one aggregate query."""
    if not organization_ids:
        return {}
    claims: dict[int, dict[str, str]] = {}
    for row in _open_claim_aggregate(organization_ids):
        claims.setdefault(row["organization_id"], {})[row["field_key"]] = _review_state(
            row["conflicted"]
        )
    return claims


def is_company_wide(row: CompanyFieldEvidence) -> bool:
    """True when the row speaks for the whole company, not a location or role.

    A scoped row (``countries`` / ``role_families``) is real evidence for its
    scope but must not certify the company-wide value: matching treats it as
    not applying outside that scope. ``claimed_domain`` is attribution, not scope.
    """
    return not list_scope(row.scope_json)


def _summary(resolved: dict[str, CompanyFieldEvidence], review: dict[str, str], now) -> dict:
    verified = stale = 0
    last_verified: datetime | None = None
    for row in resolved.values():
        if not is_company_wide(row):
            continue
        if field_status(row, now=now) == "verified":
            verified += 1
        else:
            stale += 1
        if row.last_verified_at and (last_verified is None or row.last_verified_at > last_verified):
            last_verified = row.last_verified_at
    return {
        "verified": verified,
        "stale": stale,
        "unknown": TRACKED_FIELD_COUNT - verified - stale,
        "total": TRACKED_FIELD_COUNT,
        "fact_coverage": verified + stale,
        "last_verified_at": last_verified.isoformat() if last_verified else None,
        "pending_review": len(review),
    }


def evidence_summaries_for_orgs(organization_ids, *, now: datetime | None = None) -> dict[int, dict]:
    """Per-organization fact counts, last verified date and open-claim count.

    Two queries for any number of organizations: accepted rows and an
    aggregate of open claims. Every id gets a summary, so an organization with
    no evidence reads as all-unknown rather than missing.
    """
    now = now or timezone.now()
    organization_ids = list(organization_ids)
    resolved = resolve_field_evidence_for_orgs(organization_ids, summary_only=True)
    claims = open_claims_by_org(organization_ids)
    return {
        org_id: _summary(resolved.get(org_id, {}), claims.get(org_id, {}), now)
        for org_id in organization_ids
    }


_RTO_BADGE_READINGS = {
    "r": "remote", "remote": "remote",
    "h": "hybrid", "hybrid": "hybrid",
    "o": "in-office", "in-office": "in-office", "in office": "in-office", "onsite": "in-office",
}
_VESTING_BADGE_READINGS = {
    "true": True, "yes": True, "1": True,
    "false": False, "no": False, "0": False,
}


def _badge_reading(field_key: str, text: str):
    """Canonical value of ``text`` for badge agreement, or ``None`` if ambiguous.

    Stricter than matching's readers on purpose: those rank listings and may
    guess from prose, but a "Verified" badge must never sit on a value the
    evidence contradicts. Only whole-value forms parse; any prose reads as
    ambiguous.
    """
    normalized = " ".join(text.replace("_", " ").casefold().split())
    if field_key == CompanyFieldEvidence.FieldKey.RTO_POLICY:
        return _RTO_BADGE_READINGS.get(normalized)
    if field_key == CompanyFieldEvidence.FieldKey.ACCELERATED_VESTING:
        return _VESTING_BADGE_READINGS.get(normalized)
    return normalized


def _agrees_with_displayed(field_key: str, value_text: str, shown: str | None) -> bool | None:
    """Whether the accepted evidence value says what the profile shows.

    ``None`` when the profile shows nothing or either value is ambiguous
    (prose, negations, mixed policies): the client then shows the neutral
    "Sourced value" note instead of a badge. RTO and accelerated vesting
    compare normalized readings, everything else case-folded text.
    """
    if shown is None:
        return None
    evidence_reading = _badge_reading(field_key, value_text)
    shown_reading = _badge_reading(field_key, shown)
    if evidence_reading is None or shown_reading is None:
        return None
    return evidence_reading == shown_reading


_PUBLIC_STATUS_STRICT_READINGS = frozenset(
    {"public", "public company", "private", "private company"}
)


def _strictly_readable(field_key: str, value_text: str) -> bool:
    """Whether ``value_text`` is a whole-value form matching cannot misread.

    Matching's readers guess from prose (#548), so a requirement chip may say
    "Verified" only for a value with exactly one reading: a strict RTO or
    vesting form, a bare public/private status, or a funding-round code or
    label. Prose, and fields with no strict reader, are "sourced" instead.
    """
    if field_key in (FieldKey.RTO_POLICY, FieldKey.ACCELERATED_VESTING):
        return _badge_reading(field_key, value_text) is not None
    normalized = " ".join(value_text.replace("_", " ").casefold().split())
    if field_key == FieldKey.PUBLIC_STATUS:
        return normalized in _PUBLIC_STATUS_STRICT_READINGS
    if field_key == FieldKey.FUNDING_ROUND:
        return any(
            normalized in (code.casefold(), str(label).casefold())
            for code, label in Organization.FundingRound.choices
        )
    return False


def _is_evidence_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def evidence_status_for_ids(evidence_ids, *, now: datetime | None = None) -> dict[int, dict]:
    """Read-time status of the evidence rows a stored match refers to.

    One query for any number of ids. ``state`` is ``verified`` (accepted,
    within policy, strictly readable), ``sourced`` (accepted and fresh, but
    prose matching had to interpret), ``stale``, ``superseded`` (the row is no
    longer the accepted fact) or ``missing`` (deleted). Only accepted rows
    expose ``last_verified_at`` and ``source_domain``.
    """
    ids = {pk for pk in evidence_ids if _is_evidence_id(pk)}
    if not ids:
        return {}
    now = now or timezone.now()
    statuses: dict[int, dict] = {
        pk: {"state": "missing", "last_verified_at": None, "source_domain": None}
        for pk in ids
    }
    rows = CompanyFieldEvidence.objects.filter(pk__in=ids).only(
        "field_key", "state", "value_text", "last_verified_at", "source_domain"
    )
    for row in rows:
        if row.state != State.ACCEPTED:
            statuses[row.pk]["state"] = "superseded"
            continue
        if field_status(row, now=now) == "stale":
            state = "stale"
        elif _strictly_readable(row.field_key, row.value_text):
            state = "verified"
        else:
            state = "sourced"
        statuses[row.pk] = {
            "state": state,
            "last_verified_at": (
                row.last_verified_at.isoformat() if row.last_verified_at else None
            ),
            "source_domain": row.source_domain or None,
        }
    return statuses


_PROFILE_EVIDENCE_STATUS = {
    "state": "profile",
    "last_verified_at": None,
    "source_domain": None,
}


def _requirement_evidence_status(requirement: dict, statuses: dict[int, dict]):
    source_kind = requirement.get("source_kind")
    source_id = requirement.get("source_id")
    if source_kind == "evidence":
        status = statuses.get(source_id) if _is_evidence_id(source_id) else None
        if status is None:
            return {"state": "missing", "last_verified_at": None, "source_domain": None}
        status = dict(status)
        # A row that did not decide the outcome (out of scope, or unreadable)
        # must not lend it a verified mark.
        undecided = (
            requirement.get("status") == "unknown"
            or requirement.get("scope_ok") is False
        )
        if status["state"] == "verified" and undecided:
            status["state"] = "sourced"
        return status
    if source_kind == "field" and str(source_id or "").startswith("organization."):
        return dict(_PROFILE_EVIDENCE_STATUS)
    return None


def annotate_requirement_evidence(requirement_lists, *, now: datetime | None = None) -> list[list]:
    """Copies of each requirement list with a read-time ``evidence_status``.

    At most one query for the whole response (none when no requirement cites
    an evidence row). The stored requirement dicts are never mutated: status
    depends on ``now`` and is derived per response. A requirement backed by a
    direct organization field reads ``profile``; listing data has no status.
    """
    requirement_lists = [list(requirements or []) for requirements in requirement_lists]
    evidence_ids = {
        requirement.get("source_id")
        for requirements in requirement_lists
        for requirement in requirements
        if isinstance(requirement, dict)
        and requirement.get("source_kind") == "evidence"
        and _is_evidence_id(requirement.get("source_id"))
    }
    statuses = evidence_status_for_ids(evidence_ids, now=now)
    return [
        [
            {**requirement, "evidence_status": _requirement_evidence_status(requirement, statuses)}
            if isinstance(requirement, dict)
            else requirement
            for requirement in requirements
        ]
        for requirements in requirement_lists
    ]


def field_evidence_payload(organization, *, now: datetime | None = None) -> dict:
    """Build the serialized ``fields`` / ``unverified_fields`` arrays.

    ``stale`` is computed per call from the stored timestamps and the policy
    table (never frozen into a cached flag), and every registered field key
    with no accepted evidence is named in ``unverified_fields`` — missing
    evidence is explicit. ``status`` (verified/stale) and ``review``
    (none/pending/conflicted) are separate axes: an accepted fact can be
    verified while a conflicting observation awaits review. ``pending_review``
    lists the newest open claims (capped) for inspection only; they never
    carry a verified status. A scoped accepted row never agrees with the
    company-wide value.
    """
    now = now or timezone.now()
    resolved = resolve_field_evidence(organization)
    review: dict[str, str] = {}
    pending_total = 0
    for aggregate in _open_claim_aggregate([organization.id]):
        review[aggregate["field_key"]] = _review_state(aggregate["conflicted"])
        pending_total += aggregate["total"]
    # One entry per listed claim: value, source, date and state stay together,
    # so two pages disagreeing about a field are both listed (newest first).
    # The list is capped; ``review_by_field`` and the totals stay exact.
    pending_review = [
        {
            "field_key": claim.field_key,
            "review": "conflicted" if claim.state == State.CONFLICTED else "pending",
            "observed_at": claim.observed_at.isoformat(),
            "source_domain": claim.source_domain,
            "observed_value": claim.value_text[:_MAX_VALUE_TEXT],
        }
        for claim in sorted(
            _capped_open_claims(organization.id) if pending_total else [],
            key=lambda claim: claim.field_key,
        )
    ]
    displayed = displayed_field_values(organization)
    fields = []
    for field_key in sorted(resolved):
        row = resolved[field_key]
        stale = is_stale(row.last_verified_at, now=now, field_key=row.field_key)
        fields.append(
            {
                "field_key": row.field_key,
                "state": row.state,
                "value": row.value_text,
                "source_domain": row.source_domain,
                "source_url": safe_source_url(row),
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
                "stale": stale,
                "status": "stale" if stale else "verified",
                "review": review.get(row.field_key, "none"),
                "agrees_with_displayed": (
                    _agrees_with_displayed(
                        row.field_key, row.value_text, displayed.get(row.field_key)
                    )
                    if is_company_wide(row)
                    else None
                ),
                "policy_days": FIELD_FRESHNESS_POLICY.get(
                    row.field_key, DEFAULT_FRESHNESS_DAYS
                ),
            }
        )
    unverified_fields = [key for key in FieldKey.values if key not in resolved]
    return {
        "fields": fields,
        "unverified_fields": unverified_fields,
        "summary": _summary(resolved, review, now),
        "pending_review": pending_review,
        "pending_review_total": pending_total,
        "pending_review_more": pending_total - len(pending_review),
        "review_by_field": review,
        "displayed_values": displayed,
    }


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
    "AUTO_APPLY_FIELDS",
    "DEFAULT_FRESHNESS_DAYS",
    "FIELD_FRESHNESS_POLICY",
    "OPEN_CLAIM_STATES",
    "REVIEW_REQUIRED_FIELDS",
    "EvidenceNotAcceptable",
    "accept_correction",
    "accepted_fact_changed",
    "scoped_correction_blocked",
    "accept_claim",
    "accept_observation_fields",
    "field_evidence_payload",
    "field_status",
    "safe_source_url",
    "open_claims_by_org",
    "evidence_summaries_for_orgs",
    "EVIDENCE_SCHEMA_VERSION",
    "TRACKED_FIELD_COUNT",
    "field_key_for_observation_attribute",
    "is_stale",
    "observation_field_values",
    "record_check",
    "record_claim",
    "reject_claim",
    "observe_review_field",
    "is_staff_reviewed",
    "resolve_observation_claims",
    "accepted_scope_for_claim",
    "legacy_unreviewed_rows",
    "queue_legacy_claim",
    "rejected_value_conflicts",
    "restate_open_claims",
    "scoped_accepted_conflicts",
    "value_digest",
    "update_claim_scope",
    "validate_claim_scope",
    "strip_unsafe_characters",
    "resolve_field_evidence",
    "resolve_field_evidence_for_orgs",
    "retractable_observation_facts",
    "lock_organizations",
    "matching_reading",
]


def retract_observation_facts(
    observation: CompanyProfileObservation, *, now: datetime | None = None
) -> list[int]:
    """Withdraw the review-required facts a whole-observation accept wrote.

    Rejecting an observation staff had accepted undoes that accept: the
    accepted review-required rows it created (and nobody re-accepted through a
    claim) become ``superseded``, so they stop driving matching and stop being
    re-verified. Returns the retracted ids.
    """
    if observation.organization_id is None:
        return []
    now = now or timezone.now()
    with transaction.atomic():
        _lock_organization(observation.organization_id)
        retract = retractable_observation_facts(observation, lock=True)
        if retract:
            _close_claims(retract, now)
            publication.record_event(
                target_type=PublicationEvent.TargetType.ORGANIZATION,
                target_id=observation.organization_id,
                event_kind=PublicationEvent.EventKind.CHANGED,
                payload={"status": "retracted"},
            )
    return [row.pk for row in retract]


def retractable_observation_facts(
    observation: CompanyProfileObservation, *, lock: bool = False
) -> list[CompanyFieldEvidence]:
    """Accepted review-required rows of ``observation`` no claim re-accepted.

    These are what rejecting the observation withdraws from matching.
    """
    rows = CompanyFieldEvidence.objects.filter(
        organization_id=observation.organization_id,
        observation=observation,
        state=State.ACCEPTED,
        field_key__in=sorted(REVIEW_REQUIRED_FIELDS),
    ).order_by("field_key", "id")
    if lock:
        rows = rows.select_for_update()
    by_target = {str(row.pk): row for row in rows}
    claim_accepted = set(
        OperationalChangeAudit.objects.filter(
            target_type="company_field_evidence",
            target_id__in=list(by_target),
            action="claim_accepted",
            confirmed=True,
        ).values_list("target_id", flat=True)
    )
    return [row for tid, row in by_target.items() if tid not in claim_accepted]


def lock_organizations(organization_ids) -> None:
    """Take the organization row locks in pk order (so reviewers cannot deadlock)."""
    for organization_id in sorted({pk for pk in organization_ids if pk is not None}):
        _lock_organization(organization_id)


def matching_reading(field_key: str, value_text: str) -> str:
    """How matching interprets ``value_text``, for reviewers who approve prose.

    Empty for fields matching does not parse.
    """
    from crank.agents.jobs import matching

    if field_key == CompanyFieldEvidence.FieldKey.RTO_POLICY:
        days = matching._rto_days(value_text)
        if days is None:
            return "matching cannot read this (unknown)"
        label = {0: "remote", 3: "hybrid", 5: "in-office"}[days]
        return f"matching reads this as: {label} ({days} in-office days)"
    if field_key == CompanyFieldEvidence.FieldKey.PUBLIC_STATUS:
        public = matching._public_status(value_text)
        if public is None:
            return "matching cannot read this (unknown)"
        return f"matching reads this as: {'public' if public else 'private'}"
    return ""
