# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Transactional publication outbox rows (issue #470).

``PublicationEvent`` is the durable revision record of accepted data changes.
Each writer inserts one event inside the same database transaction as the
change it describes, so a rolled-back batch writes no event and a committed
change always has one. The auto-increment ``id`` is the data revision
identity. Rows deliberately carry no foreign keys to the rows they describe so
events survive target deletion; the bounded ``payload`` holds exactly what the
sweeper needs to compute affected cache keys. Redis remains strictly a cache —
MySQL (this table) is the system of record for required publication work.
"""

from django.db import models


class PublicationEvent(models.Model):
    """One accepted data change awaiting post-commit publication."""

    class TargetType(models.TextChoices):
        ORGANIZATION = "organization", "Organization"
        SCORE = "score", "Score"
        LISTING = "listing", "Listing"

    class EventKind(models.TextChoices):
        CREATED = "created", "Created"
        CHANGED = "changed", "Changed"
        OBSERVED = "observed", "Observed"
        INGESTED = "ingested", "Ingested"

    target_type = models.CharField(max_length=16, choices=TargetType.choices)
    target_id = models.PositiveIntegerField()
    event_kind = models.CharField(max_length=16, choices=EventKind.choices)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        app_label = "crank"
        ordering = ["id"]
        indexes = [
            models.Index(
                fields=["processed_at", "id"], name="crank_pubevent_sweep_idx"
            ),
        ]

    def __str__(self):
        return f"{self.pk}:{self.target_type}:{self.target_id}:{self.event_kind}"


__all__ = ["PublicationEvent"]
