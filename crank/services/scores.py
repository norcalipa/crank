# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Idempotent, cache-aware persistence for score observations.

This is the write-side contract for Phase 2 score gathering. Normalization
(issue #313) resolves a source/type/target and produces a normalized
observation; scheduling (issue #315) drives a run. Both feed this service,
which owns the transaction boundary: it locks the relevant active score,
compares the normalized observation, treats an identical value/range/
provenance-identity as a no-op, and otherwise deactivates the prior active row
and creates exactly one replacement linked to provenance/run data.

Cache invalidation is centralized here and deferred to ``transaction.on_commit``
so a failed/rolled-back batch never invalidates caches.
"""
import logging
import re
import time
from dataclasses import dataclass

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from crank.models.agent_run import AgentRun
from crank.models.organization import Organization
from crank.models.score import Score, ScoreAlgorithmWeight, ScoreType

logger = logging.getLogger("score_persistence")

# --- Outcomes -----------------------------------------------------------------
CREATED = "created"
NOOP = "noop"
CHANGED = "changed"

# Identity keys decide whether two observations are the same provenance.
# Timestamps/raw values deliberately do NOT participate: a replay of the same
# observation (e.g. a re-fetch with a new timestamp) is still a no-op.
_PROVENANCE_IDENTITY_KEYS = ("external_id", "source_url", "adapter_version")

# Allowlist of provenance fields we are willing to persist. Unknown keys are
# dropped, so untrusted source data can never smuggle arbitrary content in.
_ALLOWED_PROVENANCE_KEYS = (
    "external_id",
    "source_url",
    "source",
    "type",
    "adapter_version",
    "observed_at",
    "fetched_at",
    "raw_value",
    "raw_low",
    "raw_high",
    "normalized_value",
    "notes",
)

# Bounds so provenance always stays small and log/DB friendly. The allowlist
# below is smaller than any cap, so per-value truncation is the effective bound.
_PROVENANCE_STRING_MAX_LENGTH = 255

# Redact anything that looks like a credential/secret before storing provenance.
_SECRET_PATTERNS = [
    re.compile(r"\b[0-9a-fA-F]{32,64}\b"),  # api keys / long hex hashes
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+"),  # bearer tokens
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),  # emails
]
_REDACTED = "<redacted>"


@dataclass(frozen=True)
class ScoreObservationResult:
    """Outcome of a persisted observation.

    ``outcome`` is one of :data:`CREATED`, :data:`NOOP`, :data:`CHANGED`.
    ``score`` is the active ``Score`` row for the ``(type, source, target)``
    tuple: the newly created row for ``created``/``changed``, or the unchanged
    existing row for ``noop``. ``replaced`` is the prior active row that was
    deactivated on a change (``None`` otherwise). ``created`` is true iff a new
    row was written.
    """

    outcome: str
    score: Score
    created: bool
    replaced: "Score | None" = None

    @property
    def changed(self):
        return self.outcome == CHANGED


# --- Cache invalidation ---------------------------------------------------------


def affected_cache_keys(target_id, score_type_id=None):
    """Return every cache key a score for ``target_id`` can invalidate.

    Covers the centralized average-score key, the organization detail/score API
    keys, and the algorithm-result keys for every algorithm that weights the
    changed score type. This is the single source of truth for score cache
    invalidation.
    """
    keys = [
        f"organization_{target_id}_avg_scores",
        f"organization_api_{target_id}",
        f"organization_scores_api_{target_id}",
    ]
    weights = ScoreAlgorithmWeight.objects.values_list(
        "algorithm_id", flat=True
    ).filter(algorithm__status=1)
    if score_type_id is not None:
        weights = weights.filter(type_id=score_type_id)
    algorithm_ids = sorted(set(weights))
    keys.extend(f"algorithm_{algorithm_id}_results" for algorithm_id in algorithm_ids)
    return keys


def invalidate_score_caches(target_id, score_type_id=None):
    """Delete every cache key affected by a score for ``target_id``.

    Safe to call even when the keys do not exist yet. Delegated invalidation is
    centralized here so callers never hand-roll cache keys.
    """
    for key in affected_cache_keys(target_id, score_type_id):
        cache.delete(key)


# --- Provenance sanitization ----------------------------------------------------


def _redact(value):
    if not isinstance(value, str):
        return value
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(_REDACTED, value)
    return value


def sanitize_provenance(provenance):
    """Return a bounded, allowlisted, secret-free copy of ``provenance``.

    Non-dict input yields ``{}``. Unknown keys are dropped, string values are
    truncated and secret-redacted, and the number of keys is capped. The result
    is JSON-friendly and never contains raw external payloads or credentials.
    """
    if not isinstance(provenance, dict):
        return {}
    cleaned = {}
    for key in _ALLOWED_PROVENANCE_KEYS:
        if key not in provenance:
            continue
        value = provenance[key]
        if isinstance(value, str):
            value = _redact(value)[:_PROVENANCE_STRING_MAX_LENGTH]
        elif not isinstance(value, (bool, int, float, type(None))):
            # Reject nested collections/objects: keep provenance flat and small
            # rather than attempt deep sanitization of untrusted payloads.
            continue
        cleaned[key] = value
    return cleaned


def _provenance_identity(provenance):
    prov = provenance if isinstance(provenance, dict) else {}
    return tuple(prov.get(key) for key in _PROVENANCE_IDENTITY_KEYS)


# --- Internal persistence -------------------------------------------------------


def _resolve_id(obj, model):
    if obj is None:
        return None  # pragma: no cover - required keyword; defensive only
    if isinstance(obj, model):
        return obj.pk
    if isinstance(obj, (int, str)):
        return int(obj)
    raise TypeError(f"Expected {model.__name__} or id, got {type(obj).__name__}")


def _identical(active, value, low_threshold, high_threshold, provenance):
    """True when ``active`` already reflects this exact normalized observation."""
    return (
        active.score == value
        and active.low_threshold == low_threshold
        and active.high_threshold == high_threshold
        and _provenance_identity(active.provenance)
        == _provenance_identity(provenance)
    )


def _persist_locked(
    *,
    source_id,
    target_id,
    score_type_id,
    value,
    low_threshold,
    high_threshold,
    provenance,
    run_id,
):
    """Lock, compare, and write a single observation within an atomic block."""
    with transaction.atomic():
        # Serialize concurrent writers for the same (type, source, target).
        # Locking the most recent row (any status) makes contenders block on a
        # single row, which preserves exactly-one-active even on MySQL where
        # Django cannot create the partial unique index.
        Score.objects.filter(
            type_id=score_type_id, source_id=source_id, target_id=target_id
        ).order_by("-id").select_for_update().first()

        active = (
            Score.objects.filter(
                type_id=score_type_id,
                source_id=source_id,
                target_id=target_id,
                status=Score.ACTIVE_STATUS,
            )
            .order_by("-id")
            .first()
        )

        if active is not None and _identical(
            active, value, low_threshold, high_threshold, provenance
        ):
            logger.info(
                "score observation noop: outcome=%s target=%s type=%s source=%s score_id=%s",
                NOOP,
                target_id,
                score_type_id,
                source_id,
                active.pk,
            )
            return ScoreObservationResult(
                outcome=NOOP, score=active, created=False, replaced=None
            )

        previous_active = None
        if active is not None:
            previous_active = active
            active.status = Score.INACTIVE_STATUS
            active.deactivate_date = timezone.now()
            active.save()

        run = None
        if run_id is not None:
            run = AgentRun.objects.filter(pk=run_id).first()

        new_score = Score.objects.create(
            type_id=score_type_id,
            source_id=source_id,
            target_id=target_id,
            score=value,
            low_threshold=low_threshold,
            high_threshold=high_threshold,
            provenance=sanitize_provenance(provenance),
            run=run,
        )
        # Invalidation fires only if the enclosing transaction commits; a
        # rolled-back batch discards these callbacks (no cache churn).
        transaction.on_commit(
            lambda _target=target_id, _type=score_type_id: invalidate_score_caches(
                _target, _type
            )
        )
        outcome = CHANGED if previous_active is not None else CREATED
        logger.info(
            "score observation %s: outcome=%s target=%s type=%s source=%s "
            "replaced_score_id=%s new_score_id=%s",
            outcome,
            outcome,
            target_id,
            score_type_id,
            source_id,
            previous_active.pk if previous_active else None,
            new_score.pk,
        )
        return ScoreObservationResult(
            outcome=outcome,
            score=new_score,
            created=True,
            replaced=previous_active,
        )


# --- Public API ----------------------------------------------------------------


def persist_score_observation(
    *,
    source,
    target,
    score_type,
    value,
    low_threshold=None,
    high_threshold=None,
    provenance=None,
    run=None,
):
    """Persist one normalized score observation transactionally.

    Parameters mirror the normalization (#313) output so scheduling (#315) can
    feed this service directly; model instances or primary keys are accepted.

    - ``source``: rating ``Organization`` (or id) that gave the score.
    - ``target``: rated ``Organization`` (or id).
    - ``score_type``: ``ScoreType`` (or id).
    - ``value``: normalized float.
    - ``low_threshold``/``high_threshold``: normalized range (default 0.0/5.0).
    - ``provenance``: sanitized dict of provenance metadata.
    - ``run``: ``AgentRun`` (or id) to link for run provenance.

    Returns a :class:`ScoreObservationResult`. Exactly one active score remains
    for the ``(type, source, target)`` tuple even under concurrent writers, and
    cache invalidation happens on commit only.
    """
    started = time.monotonic()
    source_id = _resolve_id(source, Organization)
    target_id = _resolve_id(target, Organization)
    score_type_id = _resolve_id(score_type, ScoreType)
    run_id = None if run is None else _resolve_id(run, AgentRun)
    low = low_threshold if low_threshold is not None else 0.0
    high = high_threshold if high_threshold is not None else 5.0

    try:
        result = _persist_locked(
            source_id=source_id,
            target_id=target_id,
            score_type_id=score_type_id,
            value=value,
            low_threshold=low,
            high_threshold=high,
            provenance=provenance,
            run_id=run_id,
        )
    except IntegrityError:
        # A concurrent writer created the active row between our lock check and
        # insert (first-observation race on backends that honor the partial
        # unique index). Re-enter a fresh transaction and reconcile against the
        # winner's row instead of allowing a duplicate active score.
        result = _persist_locked(
            source_id=source_id,
            target_id=target_id,
            score_type_id=score_type_id,
            value=value,
            low_threshold=low,
            high_threshold=high,
            provenance=provenance,
            run_id=run_id,
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "score observation done: outcome=%s created=%s target=%s type=%s "
        "source=%s duration_ms=%s",
        result.outcome,
        result.created,
        target_id,
        score_type_id,
        source_id,
        duration_ms,
    )
    return result
