# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Persist deterministic job-ranking results for an owner (issue #475).

The recompute path is snapshot -> compute -> CAS publish:

1. :func:`open_snapshot` locks the user's :class:`MatchResultState` row,
   decides whether the state is already current, and — if not — issues a
   monotonic ticket and reads the inventory, accepted evidence, and data
   revisions under that same lock. Every snapshot for a user therefore takes
   the same row lock, so snapshots are totally ordered: ticket order equals
   preference order equals data order.
2. Compute (:func:`crank.agents.jobs.matching.rank_listings`) happens with no
   transaction or lock held.
3. :func:`publish` re-locks the state row and performs the compare-and-swap:
   a ticket at or below the row's ``current_generation`` is discarded, so an
   older run that snapshotted first but publishes last can never overwrite a
   newer committed generation.

``persist_matches`` is the legacy wrapper: snapshot (ticket + revision +
watermark) -> rank the **supplied** inputs -> publish. It has no production
caller after issue #475 (the pipeline, the preference hook, and the drain
all call :mod:`crank.services.match_recompute`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from crank.agents.jobs.matching import project_criteria, rank_listings
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.preference import UserPreference
from crank.services.company_evidence import resolve_field_evidence_for_orgs
from crank.services.publication import data_watermark, listing_data_revisions

logger = logging.getLogger(__name__)

_UPDATE_FIELDS = [
    "organization",
    "score",
    "factors",
    "preference_revision",
    "data_revision",
    "generated_at",
    "requirements",
    "evidence_ids",
    "last_matched_at",
    "result_generation",
    "modified",
]


class PublishOutcome(str, Enum):
    """Outcome of a :func:`publish` (or a short-circuited :func:`open_snapshot`)."""

    PUBLISHED = "published"
    CURRENT = "current"
    DISCARDED_STALE = "discarded_stale"
    NO_PREFERENCES = "no_preferences"
    USER_GONE = "user_gone"
    FAILED = "failed"


def _factor_data(factors):
    """Convert ranking factor dataclasses into JSON-safe dictionaries."""
    return [
        {
            "factor": factor.factor,
            "score": factor.score,
            "max_score": factor.max_score,
            "detail": factor.detail,
        }
        for factor in factors
    ]


def _org_id(listing):
    organization = getattr(listing, "organization", None)
    if organization is not None and getattr(organization, "pk", None):
        return int(organization.pk)
    return None


def _preference_revision(user):
    """The user's current document revision, or ``None`` when absent."""
    try:
        return UserPreference.objects.get(user=user).revision
    except UserPreference.DoesNotExist:
        return None


def match_inventory(limit):
    """Active listings from approved+enabled sources, newest first, bounded.

    Moved from ``crank.services.job_pipeline._active_listings`` (issue #475)
    so the pipeline and the recompute snapshot share one inventory query.
    """
    return list(
        JobListing.objects.filter(
            status=JobListing.Status.ACTIVE,
            source__approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            source__enabled=True,
        )
        .select_related("organization")
        .order_by("-last_seen_at", "pk")[:limit]
    )


@dataclass(frozen=True)
class MatchSnapshot:
    """A totally-ordered snapshot of one user's recompute inputs."""

    user_id: int
    ticket: int
    preference_revision: int | None
    preference_version: int | None
    ranker_version: str
    data_revision: int | None
    listings: list = field(default_factory=list)
    criteria: Any = None
    evidence: dict = field(default_factory=dict)
    data_revisions: dict = field(default_factory=dict)


def _within_age(generated_at, max_age_hours):
    if generated_at is None:
        return False
    if not max_age_hours:
        # 0 (or unset) disables the age backstop: an existing generation
        # never ages out on its own.
        return True
    return generated_at >= timezone.now() - timezone.timedelta(hours=max_age_hours)


def _get_or_create_state(user):
    """Race-safely fetch or create the user's state row (savepoint pattern,
    following ``crank.services.preferences._create_or_fetch``)."""
    state = MatchResultState.objects.filter(user=user).first()
    if state is not None:
        return state
    try:
        with transaction.atomic():
            return MatchResultState.objects.create(user=user)
    except Exception:  # noqa: BLE001 - IntegrityError on the unique user FK
        return MatchResultState.objects.get(user=user)


def ensure_match_result_state(user):
    """Public wrapper for :func:`_get_or_create_state` (issue #475 review):
    lets callers outside this module (the seen/dismiss views) take the same
    state-row lock that :func:`publish` uses, so dismiss/seen updates are
    serialized with a concurrent publish."""
    return _get_or_create_state(user)


def open_snapshot(
    user,
    *,
    config=DEFAULT_CONFIG,
    max_listings,
    max_age_hours=None,
    force=False,
):
    """Snapshot phase: lock the state row, then decide CURRENT vs. issue a ticket.

    Returns a :class:`MatchSnapshot` when a recompute is needed,
    ``PublishOutcome.CURRENT`` when the existing generation is already
    current (no ticket issued, no inventory read — duplicate work avoided),
    or ``PublishOutcome.NO_PREFERENCES`` when the user has no
    ``UserPreference`` row. ``force=True`` skips the currency check.
    """
    _get_or_create_state(user)
    with transaction.atomic():
        state = MatchResultState.objects.select_for_update().get(user=user)
        pref = UserPreference.objects.filter(user=user).first()
        if pref is None:
            return PublishOutcome.NO_PREFERENCES

        watermark = data_watermark()
        current = (
            not force
            and state.current_generation is not None
            and state.preference_revision == pref.revision
            and state.preference_version == pref.schema_version
            and state.ranker_version == config.version
            and (state.data_revision or 0) >= (watermark or 0)
            and _within_age(state.generated_at, max_age_hours)
        )
        if current:
            return PublishOutcome.CURRENT

        ticket = state.issued_generation + 1
        MatchResultState.objects.filter(pk=state.pk).update(
            issued_generation=F("issued_generation") + 1
        )

        listings = match_inventory(max_listings)
        org_ids = {
            oid for oid in (_org_id(listing) for listing in listings) if oid is not None
        }
        evidence = resolve_field_evidence_for_orgs(org_ids)
        data_revisions = listing_data_revisions(listings)
        criteria = project_criteria(pref.preferences, pref.schema_version)

        return MatchSnapshot(
            user_id=user.pk,
            ticket=ticket,
            preference_revision=pref.revision,
            preference_version=pref.schema_version,
            ranker_version=config.version,
            data_revision=watermark,
            listings=listings,
            criteria=criteria,
            evidence=evidence,
            data_revisions=data_revisions,
        )


def _carry_forward_rows(user_id, listing_ids):
    """Map ``listing_id -> most-recently-modified existing row`` for carry-forward."""
    carry = {}
    for match in (
        JobMatch.objects.filter(user_id=user_id, listing_id__in=listing_ids)
        .order_by("listing_id", "-modified", "-id")
    ):
        carry.setdefault(match.listing_id, match)
    return carry


def _publish_inner(snapshot, ranked):
    state = MatchResultState.objects.select_for_update().filter(
        user_id=snapshot.user_id
    ).first()
    if state is None:
        return PublishOutcome.USER_GONE
    if snapshot.ticket <= (state.current_generation or 0):
        return PublishOutcome.DISCARDED_STALE
    if (
        state.preference_revision is not None
        and snapshot.preference_revision is not None
        and snapshot.preference_revision < state.preference_revision
    ):
        return PublishOutcome.DISCARDED_STALE

    listing_by_id = {listing.pk: listing for listing in snapshot.listings}
    matched = [result for result in ranked if not result.excluded]
    matched_listing_ids = [result.listing_id for result in matched]
    existing_listing_ids = set(
        JobListing.objects.filter(pk__in=matched_listing_ids).values_list("pk", flat=True)
    )
    existing_by_key = {
        (match.listing_id, match.preference_version, match.ranker_version): match
        for match in JobMatch.objects.filter(
            user_id=snapshot.user_id, listing_id__in=matched_listing_ids
        )
    }
    carry_forward = _carry_forward_rows(snapshot.user_id, matched_listing_ids)

    now = timezone.now()
    to_update = []
    to_create = []
    count = 0
    for result in matched:
        if result.listing_id not in existing_listing_ids:
            continue
        listing = listing_by_id.get(result.listing_id)
        if listing is None:
            continue
        org_id = _org_id(listing)
        org_evidence = snapshot.evidence.get(org_id) or {} if org_id is not None else {}
        evidence_ids = sorted(
            {ev.pk for ev in org_evidence.values() if getattr(ev, "pk", None) is not None}
        )
        data_revision = snapshot.data_revisions.get(listing.pk)
        factors = _factor_data(result.factors)
        requirements = [outcome.as_dict() for outcome in result.requirements]
        key = (result.listing_id, result.criteria_version, result.ranker_version)
        existing = existing_by_key.get(key)
        if existing is not None:
            existing.organization = listing.organization
            existing.score = result.score
            existing.factors = factors
            existing.preference_revision = snapshot.preference_revision
            existing.data_revision = data_revision
            existing.generated_at = now
            existing.requirements = requirements
            existing.evidence_ids = evidence_ids
            existing.last_matched_at = now
            existing.result_generation = snapshot.ticket
            existing.modified = now
            to_update.append(existing)
        else:
            carry = carry_forward.get(result.listing_id)
            to_create.append(
                JobMatch(
                    user_id=snapshot.user_id,
                    listing=listing,
                    organization=listing.organization,
                    preference_version=result.criteria_version,
                    ranker_version=result.ranker_version,
                    score=result.score,
                    factors=factors,
                    preference_revision=snapshot.preference_revision,
                    data_revision=data_revision,
                    generated_at=now,
                    requirements=requirements,
                    evidence_ids=evidence_ids,
                    first_matched_at=carry.first_matched_at if carry else now,
                    last_matched_at=now,
                    seen_at=carry.seen_at if carry else None,
                    dismissed=carry.dismissed if carry else False,
                    result_generation=snapshot.ticket,
                )
            )
        count += 1

    if to_update:
        JobMatch.objects.bulk_update(to_update, _UPDATE_FIELDS)
    if to_create:
        JobMatch.objects.bulk_create(to_create)

    MatchResultState.objects.filter(pk=state.pk).update(
        current_generation=snapshot.ticket,
        preference_revision=snapshot.preference_revision,
        preference_version=snapshot.preference_version,
        ranker_version=snapshot.ranker_version,
        data_revision=snapshot.data_revision,
        generated_at=now,
        result_count=count,
    )
    return PublishOutcome.PUBLISHED


def publish(snapshot, ranked):
    """Publish phase (the CAS): lock the state row and try to win the swap.

    Any exception rolls back the whole publish (state and rows unchanged)
    and returns ``PublishOutcome.FAILED``; the user stays dirty for the next
    drain to retry.
    """
    try:
        with transaction.atomic():
            return _publish_inner(snapshot, ranked)
    except Exception:  # noqa: BLE001 - the publish boundary never raises
        logger.exception(
            "match publish failed; user stays dirty for retry "
            "(user_id=%s, generation=%s)",
            getattr(snapshot, "user_id", None),
            getattr(snapshot, "ticket", None),
        )
        return PublishOutcome.FAILED


def persist_matches(user, listings, criteria, config):
    """Compatibility wrapper: snapshot (ticket+revision+watermark) -> rank the
    **supplied** inputs -> publish.

    Has no production caller after issue #475 (the pipeline, the preference
    hook, and the drain all call :mod:`crank.services.match_recompute`
    instead). Kept for existing tests and any direct scripted use. It must
    still publish when the user has no ``UserPreference`` row, stamping
    ``preference_revision=None`` the way the legacy per-row upsert did.
    """
    active_status = JobListing.Status.ACTIVE
    listings = [listing for listing in listings if listing.status == active_status]
    org_ids = {_org_id(listing) for listing in listings if _org_id(listing) is not None}
    evidence = resolve_field_evidence_for_orgs(org_ids)
    data_revisions = listing_data_revisions(listings)
    preference_revision = _preference_revision(user)
    watermark = data_watermark()

    ranked = rank_listings(listings, criteria, config, evidence=evidence)

    _get_or_create_state(user)
    with transaction.atomic():
        state = MatchResultState.objects.select_for_update().get(user=user)
        ticket = state.issued_generation + 1
        MatchResultState.objects.filter(pk=state.pk).update(
            issued_generation=F("issued_generation") + 1
        )

    snapshot = MatchSnapshot(
        user_id=user.pk,
        ticket=ticket,
        preference_revision=preference_revision,
        preference_version=getattr(criteria, "criteria_version", None),
        ranker_version=config.version,
        data_revision=watermark,
        listings=listings,
        criteria=criteria,
        evidence=evidence,
        data_revisions=data_revisions,
    )
    outcome = publish(snapshot, ranked)
    if outcome != PublishOutcome.PUBLISHED:
        return 0
    return (
        MatchResultState.objects.filter(user=user)
        .values_list("result_count", flat=True)
        .first()
        or 0
    )


__all__ = [
    "MatchSnapshot",
    "PublishOutcome",
    "ensure_match_result_state",
    "match_inventory",
    "open_snapshot",
    "persist_matches",
    "publish",
]
