# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Transactional publication outbox service (issue #470).

Write side: ``record_event`` inserts one bounded ``PublicationEvent`` inside
the caller's transaction, in the same transaction as the accepted change it
describes, so the event commits exactly when the data commits and a
rolled-back batch leaves no event. Consume side: ``sweep_pending`` processes
pending events ordered by id, unions the affected cache keys across every
selected event (pending events for the same target can carry different
affected-key sets, e.g. score types or id chunks), deletes the deduplicated
key set once, and marks processed. It is idempotent and crash-safe: cache
keys are deleted *before* ``processed_at`` is set, so a crash at any point
only ever re-runs idempotent deletions. Redis is strictly a cache; this
table is the durable record of required publication work.
"""

import logging

from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.utils import timezone

from crank.models.publication import PublicationEvent

logger = logging.getLogger("publication")

# Payload allowlist: only these keys are ever persisted, so untrusted source
# data can never smuggle arbitrary content into the outbox. Everything is
# scalar or a bounded int list; strings are truncated.
_PAYLOAD_INT_KEYS = frozenset(
    {
        "score_type_id",
        "source_id",
        "observation_id",
        "ingested",
        "updated",
        "resolved",
        "unresolved",
        "chunk_index",
        "chunk_count",
    }
)
_PAYLOAD_STRING_KEYS = frozenset({"outcome", "source_key", "status"})
_PAYLOAD_LIST_KEYS = frozenset({"organization_ids"})
_PAYLOAD_STRING_MAX_LENGTH = 255
_MAX_PAYLOAD_KEYS = 8
# Per-event bound for ``organization_ids``. Payload lists are bounded by
# construction, never silently truncated: writers that need more ids emit
# chunked events (``chunk_index``/``chunk_count``) so every organization is
# published. An oversized list fails loudly instead of dropping
# invalidations.
MAX_PAYLOAD_ORGANIZATION_IDS = 200


def _clean_payload(payload):
    """Return a bounded, allowlisted copy of ``payload``.

    Non-dict input yields ``{}``. Unknown keys are dropped, ints are kept only
    under integer keys (bools are rejected), strings are truncated, and
    ``organization_ids`` keeps every valid int but raises ``ValueError`` when
    the list exceeds ``MAX_PAYLOAD_ORGANIZATION_IDS`` — a list larger than one
    payload chunk must be split into chunked events by the writer, never
    silently truncated here. The result is JSON-friendly and never carries
    user content or credentials.
    """
    if not isinstance(payload, dict):
        return {}
    cleaned = {}
    for key, value in payload.items():
        if len(cleaned) >= _MAX_PAYLOAD_KEYS:
            break
        if key in _PAYLOAD_INT_KEYS and isinstance(value, int) and not isinstance(value, bool):
            cleaned[key] = value
        elif key in _PAYLOAD_STRING_KEYS and isinstance(value, str):
            cleaned[key] = value[:_PAYLOAD_STRING_MAX_LENGTH]
        elif key in _PAYLOAD_LIST_KEYS and isinstance(value, (list, tuple)):
            ids = []
            for item in value:
                if isinstance(item, int) and not isinstance(item, bool):
                    ids.append(item)
            if len(ids) > MAX_PAYLOAD_ORGANIZATION_IDS:
                raise ValueError(
                    "organization_ids exceeds the per-event bound of "
                    f"{MAX_PAYLOAD_ORGANIZATION_IDS}; emit chunked events "
                    "instead of dropping organization invalidations"
                )
            cleaned[key] = ids
    return cleaned


def record_event(*, target_type, target_id, event_kind, payload=None):
    """Record one accepted change for post-commit publication.

    Must be called inside the same ``transaction.atomic()`` block as the data
    change it describes (the writers own that boundary); the event row then
    commits exactly when the change commits. Returns the created
    ``PublicationEvent`` whose auto-increment ``id`` is the revision identity.
    Invalid target types, event kinds, or ids raise before any write so a bad
    caller fails loudly instead of silently skipping publication.
    """
    if target_type not in PublicationEvent.TargetType.values:
        raise ValueError(f"Unsupported publication target type: {target_type!r}")
    if event_kind not in PublicationEvent.EventKind.values:
        raise ValueError(f"Unsupported publication event kind: {event_kind!r}")
    target_id = int(target_id)
    if target_id < 0:
        raise ValueError("target_id must be a non-negative integer")
    return PublicationEvent.objects.create(
        target_type=target_type,
        target_id=target_id,
        event_kind=event_kind,
        payload=_clean_payload(payload),
    )


def affected_keys(event):
    """Return every cache key the event's target can invalidate.

    Delegates to ``scores.affected_cache_keys`` — the single source of truth
    for organization/score cache keys (including the provenance key). The
    import is lazy because ``crank.services.scores`` imports this module to
    record events. Score events narrow algorithm keys via the payload's
    ``score_type_id``; organization and listing events invalidate all
    organization-scope keys for each affected organization. Invalidation-only
    behavior can over-invalidate but never under-invalidate.
    """
    from crank.services import scores

    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.target_type == PublicationEvent.TargetType.SCORE:
        return scores.affected_cache_keys(event.target_id, payload.get("score_type_id"))
    if event.target_type == PublicationEvent.TargetType.LISTING:
        keys = []
        for organization_id in payload.get("organization_ids", []) or []:
            keys.extend(scores.affected_cache_keys(int(organization_id), None))
        return keys
    return scores.affected_cache_keys(event.target_id, None)


def sweep_pending(limit=None):
    """Process pending publication events; return bounded counts.

    Orders pending events by id, unions the affected cache keys across every
    selected event — pending events for the same ``(target_type, target_id)``
    can carry different affected-key sets (score events with different
    ``score_type_id``s weight different algorithms; chunked listing events
    carry different organization ids), so collapsing to the latest payload
    would drop required invalidations — deletes the deduplicated key set
    once, then marks the swept rows processed with a conditional update so a
    concurrent sweep can neither double-count nor un-process rows. A crash
    before the update leaves rows pending; the next sweep re-runs the
    idempotent cache deletions.
    """
    limit = max(
        1, int(limit or getattr(settings, "PUBLICATION_SWEEP_BATCH_SIZE", 500))
    )
    batch = list(
        PublicationEvent.objects.filter(processed_at__isnull=True)
        .order_by("id")
        .values("id", "target_type", "target_id", "event_kind", "payload")[:limit]
    )
    counts = {"scanned": len(batch), "processed": 0, "keys_deleted": 0}
    if not batch:
        return counts
    # Union the affected keys of every selected event: keeping only the
    # latest payload per target would lose required invalidations when
    # events for the same target carry different key dimensions.
    keys = set()
    for event in batch:
        keys.update(
            affected_keys(
                PublicationEvent(
                    target_type=event["target_type"],
                    target_id=event["target_id"],
                    event_kind=event["event_kind"],
                    payload=event["payload"],
                )
            )
        )
    for key in sorted(keys):
        cache.delete(key)
    counts["keys_deleted"] = len(keys)
    counts["processed"] = PublicationEvent.objects.filter(
        models.Q(id__in=[event["id"] for event in batch]),
        processed_at__isnull=True,
    ).update(processed_at=timezone.now())
    logger.info(
        "publication sweep: scanned=%s processed=%s keys_deleted=%s",
        counts["scanned"],
        counts["processed"],
        counts["keys_deleted"],
    )
    return counts


def consumer_enabled():
    """Whether the publication consumer may run.

    Gated by the ``PUBLICATION_CONSUMER_ENABLED`` settings flag (default
    False) and the ``publication_consumer`` ``CapabilitySwitch``: with the
    flag off the consumer never runs; with the flag on the switch decides
    (absent switch defaults to enabled). Either control independently stops
    the consumer, and pending events simply accumulate when it is off.
    """
    from crank.services import monitoring

    if not getattr(settings, "PUBLICATION_CONSUMER_ENABLED", False):
        return False
    return monitoring.capability_enabled("publication_consumer", default=True)


def _max_revision_ids(target_type, target_ids):
    """Map each target id to its latest ``PublicationEvent.id`` (or omit it)."""
    ids = {int(i) for i in target_ids if i is not None}
    if not ids:
        return {}
    rows = (
        PublicationEvent.objects.filter(target_type=target_type, target_id__in=ids)
        .values("target_id")
        .annotate(max_id=models.Max("id"))
    )
    return {int(row["target_id"]): int(row["max_id"]) for row in rows}


def listing_data_revisions(listings):
    """Return ``{listing_id: data_revision}`` for a list of listing objects.

    A listing's data revision is the greatest ``PublicationEvent.id`` among
    the ORGANIZATION and SCORE events of the listing's organization, and the
    LISTING events of the listing's **source** (issue #475 G3 fix). The only
    LISTING-event writer, ``record_source_publication``
    (``crank/services/job_ingest.py``), writes ``target_id=source.pk`` — one
    event covers every listing ingested from that source in the same
    transaction — so LISTING events must be looked up by source id, never by
    listing id (the earlier keyspace mismatch, issue #467 AC-8). SCORE
    events are also included because organization scores feed the fit score
    (``_score_organization``, ``crank/agents/jobs/matching.py``) but were
    previously ignored. Single shared helper used by both the matching
    service and the persistence layer.
    """
    listings = list(listings)
    org_ids = {
        int(getattr(getattr(l, "organization", None), "pk", 0) or 0)
        for l in listings
    }
    source_ids = {int(getattr(l, "source_id", 0) or 0) for l in listings}
    org_rev = _max_revision_ids(PublicationEvent.TargetType.ORGANIZATION, org_ids)
    score_rev = _max_revision_ids(PublicationEvent.TargetType.SCORE, org_ids)
    listing_rev = _max_revision_ids(PublicationEvent.TargetType.LISTING, source_ids)
    result: dict[int, int | None] = {}
    for listing in listings:
        lid = int(getattr(listing, "pk", 0) or 0)
        org = getattr(listing, "organization", None)
        oid = int(getattr(org, "pk", 0) or 0) if org is not None else 0
        sid = int(getattr(listing, "source_id", 0) or 0)
        candidates = [
            v
            for v in (org_rev.get(oid), score_rev.get(oid), listing_rev.get(sid))
            if v
        ]
        result[lid] = max(candidates) if candidates else None
    return result


def organization_data_revisions(organization_ids):
    """Map each organization id to its latest ORGANIZATION or SCORE ``PublicationEvent.id``."""
    org_ids = {int(i) for i in organization_ids if i is not None}
    org_rev = _max_revision_ids(PublicationEvent.TargetType.ORGANIZATION, org_ids)
    score_rev = _max_revision_ids(PublicationEvent.TargetType.SCORE, org_ids)
    result: dict[int, int] = {}
    for oid in org_ids:
        candidates = [v for v in (org_rev.get(oid), score_rev.get(oid)) if v]
        if candidates:
            result[oid] = max(candidates)
    return result


def data_watermark():
    """Return the highest ``PublicationEvent.id`` recorded so far, or ``None``.

    Used by the recompute drain (issue #475) as a coarse dirtiness signal:
    any user whose generation was computed before this watermark may be
    missing a committed data change. See ``docs/match-recompute.md`` for the
    residual gap this leaves (an event allocated before, but committed
    after, a snapshot that already observed a higher id) and how the age
    backstop bounds it.
    """
    return PublicationEvent.objects.aggregate(watermark=models.Max("id"))["watermark"]


__all__ = [
    "MAX_PAYLOAD_ORGANIZATION_IDS",
    "affected_keys",
    "consumer_enabled",
    "data_watermark",
    "listing_data_revisions",
    "organization_data_revisions",
    "record_event",
    "sweep_pending",
]
