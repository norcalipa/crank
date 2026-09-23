# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Durable, versioned job-match recomputation (issue #475).

Ties the snapshot -> compute -> CAS-publish primitives in
:mod:`crank.agents.jobs.match_persist` to three triggers:

- ``on_preference_committed`` — the fast path fired by the #466 preference
  hook (:func:`crank.services.preferences._schedule_recompute`) after a
  committed preference change.
- :func:`drain` — the bounded ``recompute_matches`` management command.
- The job pipeline's ``_run_user`` (``crank/services/job_pipeline.py``),
  which calls :func:`recompute_user` directly.

There is no request queue: dirtiness is derived from the durable
``UserPreference`` row and the ``MatchResultState`` tags, so N committed
events for one user collapse into exactly one recompute (:func:`pending_users`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import F, Q

from crank.agents.jobs.match_persist import (
    PublishOutcome,
    open_snapshot,
    publish,
)
from crank.agents.jobs.matching import rank_listings
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG
from crank.models.job_match import MatchResultState
from crank.models.preference import UserPreference
from crank.services import publication

logger = logging.getLogger("match_recompute")


class RecomputeStatus(str, Enum):
    PUBLISHED = "published"
    CURRENT = "current"
    DISCARDED_STALE = "discarded_stale"
    NO_PREFERENCES = "no_preferences"
    USER_GONE = "user_gone"
    FAILED = "failed"


@dataclass(frozen=True)
class RecomputeOutcome:
    status: RecomputeStatus
    generation: int | None = None
    persisted: int = 0


_OUTCOME_MAP = {
    PublishOutcome.PUBLISHED: RecomputeStatus.PUBLISHED,
    PublishOutcome.CURRENT: RecomputeStatus.CURRENT,
    PublishOutcome.DISCARDED_STALE: RecomputeStatus.DISCARDED_STALE,
    PublishOutcome.NO_PREFERENCES: RecomputeStatus.NO_PREFERENCES,
    PublishOutcome.USER_GONE: RecomputeStatus.USER_GONE,
    PublishOutcome.FAILED: RecomputeStatus.FAILED,
}


def recompute_enabled():
    """Whether the inline preference-hook fast path may run.

    Gated by ``MATCH_RECOMPUTE_ENABLED`` (default False) plus the
    ``match_recompute`` ``CapabilitySwitch`` (absent switch defaults to
    enabled once the settings flag is on).
    """
    from crank.services import monitoring

    if not getattr(settings, "MATCH_RECOMPUTE_ENABLED", False):
        return False
    return monitoring.capability_enabled("match_recompute", default=True)


def _max_listings():
    return max(1, int(getattr(settings, "JOB_PIPELINE_MAX_LISTINGS_PER_USER", 500)))


def _max_age_hours():
    return int(getattr(settings, "MATCH_RECOMPUTE_MAX_AGE_HOURS", 24))


def recompute_user(user_or_id, *, reason, config=DEFAULT_CONFIG, force=False):
    """Run one snapshot -> compute -> publish cycle for one user.

    ``reason`` is a free-form string for logs only ("preference", "pipeline",
    "drain"). Returns a :class:`RecomputeOutcome`. Never raises: any failure
    surfaces as ``RecomputeStatus.FAILED`` so callers (the hook, the pipeline,
    the drain) can log and move on without depending on this succeeding.
    """
    if isinstance(user_or_id, int):
        User = get_user_model()
        try:
            user = User.objects.get(pk=user_or_id)
        except User.DoesNotExist:
            return RecomputeOutcome(status=RecomputeStatus.USER_GONE)
    else:
        user = user_or_id

    try:
        snapshot_or_outcome = open_snapshot(
            user,
            config=config,
            max_listings=_max_listings(),
            max_age_hours=_max_age_hours(),
            force=force,
        )
        if not hasattr(snapshot_or_outcome, "ticket"):
            # A PublishOutcome short-circuit: CURRENT or NO_PREFERENCES.
            return RecomputeOutcome(status=_OUTCOME_MAP[snapshot_or_outcome])

        snapshot = snapshot_or_outcome
        ranked = rank_listings(
            snapshot.listings,
            snapshot.criteria,
            config,
            evidence=snapshot.evidence,
        )
        outcome = publish(snapshot, ranked)
        status = _OUTCOME_MAP[outcome]
        persisted = 0
        if status == RecomputeStatus.PUBLISHED:
            persisted = (
                MatchResultState.objects.filter(user=user)
                .values_list("result_count", flat=True)
                .first()
                or 0
            )
        return RecomputeOutcome(
            status=status, generation=snapshot.ticket, persisted=persisted
        )
    except Exception:  # noqa: BLE001 - recompute must never raise into callers
        logger.exception(
            "match recompute failed: user_id=%s reason=%s",
            getattr(user, "pk", user_or_id),
            reason,
        )
        return RecomputeOutcome(status=RecomputeStatus.FAILED)


def pending_users(limit):
    """Users needing recompute, preference-dirty first, then generation-dirty.

    (a) Preference-dirty: no state row, no current generation, revision or
    schema-version mismatch — ordered by oldest ``UserPreference.modified``.
    (b) Generation-dirty: stale ranker version, data watermark behind, or the
    generation is older than ``MATCH_RECOMPUTE_MAX_AGE_HOURS`` — ordered by
    oldest ``generated_at``. Users whose ``UserPreference`` row is gone are
    excluded from (b): ``recompute_user`` returns ``NO_PREFERENCES`` for them
    without ever advancing their state, so including them would spend a
    drain slot on the same user forever. The preference row is the durable
    "request": a committed save can never be lost even if the fast path
    crashes.
    """
    limit = max(0, int(limit))
    if limit == 0:
        return []

    # Selected entirely in SQL (not scanned-and-capped in Python) so a dirty
    # user is never starved by however many clean users are older: an
    # unmatched relation (no state row) is caught by ``no_state``, and a
    # mismatched tag is caught by ``mismatch`` via a cross-row F() compare.
    no_state = Q(user__match_result_state__isnull=True)
    mismatch = (
        Q(user__match_result_state__current_generation__isnull=True)
        | ~Q(user__match_result_state__preference_revision=F("revision"))
        | ~Q(user__match_result_state__preference_version=F("schema_version"))
    )
    refined = list(
        UserPreference.objects.filter(no_state | mismatch)
        .order_by("modified")
        .values_list("user_id", flat=True)[:limit]
    )

    remaining = limit - len(refined)
    if remaining <= 0:
        return refined[:limit]

    watermark = publication.data_watermark() or 0
    max_age = _max_age_hours()
    from django.utils import timezone

    age_cutoff = (
        timezone.now() - timezone.timedelta(hours=max_age) if max_age else None
    )
    generation_dirty_qs = (
        MatchResultState.objects.filter(current_generation__isnull=False)
        .exclude(user_id__in=refined)
        .exclude(user__preferences__isnull=True)
    )
    if watermark:
        stale_data = Q(data_revision__isnull=True) | Q(data_revision__lt=watermark)
    else:
        stale_data = Q(pk__in=[])
    version_mismatch = ~Q(ranker_version=DEFAULT_CONFIG.version)
    # A ticket issued but never published (a snapshot whose publish crashed
    # or failed) marks the user dirty for retry even when every tag still
    # matches — the durable signal a purely transient publish failure needs.
    interrupted_run = Q(issued_generation__gt=F("current_generation"))
    conditions = stale_data | version_mismatch | interrupted_run
    if age_cutoff is not None:
        conditions |= Q(generated_at__lt=age_cutoff)
    generation_dirty = list(
        generation_dirty_qs.filter(conditions)
        .order_by("generated_at")
        .values_list("user_id", flat=True)[:remaining]
    )
    return refined + generation_dirty


def drain(limit, deadline=None):
    """Process ``pending_users(limit)`` until ``deadline`` (monotonic seconds).

    Returns a counts dict. Catches per-user errors so one bad user never
    stops the batch. ``duplicate_skipped`` counts users already
    ``CURRENT`` (no-op recomputes); ``stale_discarded`` counts publishes a
    newer generation already won.
    """
    import time

    User = get_user_model()
    counts = {
        "users_total": 0,
        "users_succeeded": 0,
        "users_failed": 0,
        "matches_persisted": 0,
        "stale_discarded": 0,
        "duplicate_skipped": 0,
        "deadline_reached": False,
    }
    user_ids = pending_users(limit)
    counts["users_total"] = len(user_ids)
    users_by_id = {u.pk: u for u in User.objects.filter(pk__in=user_ids)}
    for user_id in user_ids:
        if deadline is not None and time.monotonic() >= deadline:
            counts["deadline_reached"] = True
            break
        user = users_by_id.get(user_id)
        if user is None:
            continue
        outcome = recompute_user(user, reason="drain")
        if outcome.status == RecomputeStatus.PUBLISHED:
            counts["users_succeeded"] += 1
            counts["matches_persisted"] += outcome.persisted
        elif outcome.status == RecomputeStatus.CURRENT:
            counts["users_succeeded"] += 1
            counts["duplicate_skipped"] += 1
        elif outcome.status == RecomputeStatus.DISCARDED_STALE:
            counts["users_succeeded"] += 1
            counts["stale_discarded"] += 1
        elif outcome.status in (
            RecomputeStatus.NO_PREFERENCES,
            RecomputeStatus.USER_GONE,
        ):
            counts["users_succeeded"] += 1
        else:
            counts["users_failed"] += 1
    return counts


def on_preference_committed(change_id):
    """The #466 hook target: ``change_id = "{user_id}:{revision}"``.

    Runs inline after a committed preference change when
    :func:`recompute_enabled` is true. The #466 ``_fire`` wrapper already
    logs and swallows any exception this raises, so preference saves never
    depend on this succeeding; the drain remains the durable fallback.
    """
    if not recompute_enabled():
        return
    try:
        user_id = int(str(change_id).split(":", 1)[0])
    except (TypeError, ValueError):
        logger.warning("match recompute hook: unparseable change_id=%r", change_id)
        return
    recompute_user(user_id, reason="preference")


__all__ = [
    "RecomputeOutcome",
    "RecomputeStatus",
    "drain",
    "on_preference_committed",
    "pending_users",
    "recompute_enabled",
    "recompute_user",
]
