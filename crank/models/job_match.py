# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Persisted, owner-scoped job matches."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from crank.models.job import JobListing
from crank.models.organization import Organization


class JobMatch(TimeStampedModel):
    """Owner-scoped, deduplicated match linking a user to a listing."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_matches",
    )
    listing = models.ForeignKey(
        JobListing,
        on_delete=models.CASCADE,
        related_name="matches",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_matches",
    )
    preference_version = models.PositiveIntegerField()
    ranker_version = models.CharField(max_length=32)
    score = models.FloatField()
    factors = models.JSONField(default=list, blank=True)
    # issue #467: additive, nullable result-revision columns. The unique key and
    # ``preference_version`` (schema version) are unchanged; these names record
    # the *values* a result was computed from without entering the key.
    preference_revision = models.PositiveBigIntegerField(null=True, blank=True)
    data_revision = models.PositiveBigIntegerField(null=True, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    requirements = models.JSONField(default=list, blank=True)
    evidence_ids = models.JSONField(default=list, blank=True)
    first_matched_at = models.DateTimeField()
    last_matched_at = models.DateTimeField()
    seen_at = models.DateTimeField(null=True, blank=True)
    dismissed = models.BooleanField(default=False, db_index=True)
    # issue #475: the committed generation (``MatchResultState.current_generation``)
    # that last (re)produced this row. ``NULL`` marks a pre-#475 row, served by the
    # live fallback until the drain publishes a generation for its owner. This is
    # additive: the unique key and version columns above are unchanged.
    result_generation = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        app_label = "crank"
        ordering = ["-score", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "listing", "preference_version", "ranker_version"],
                name="unique_job_match_version",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "dismissed", "-score"],
                name="crank_jobmatch_user_idx",
            ),
            models.Index(
                fields=["user", "result_generation"],
                name="crank_jobmatch_user_gen_idx",
            ),
        ]

    def __str__(self):
        return f"{self.listing} for {self.user}"


class MatchResultState(TimeStampedModel):
    """One row per user tracking the committed job-match "generation" (issue #475).

    ``issued_generation`` is a monotonic ticket counter bumped under the row
    lock every time a snapshot is taken; ``current_generation`` is the ticket
    of the last *published* (CAS-won) generation, together with the tags
    (``preference_revision``, ``preference_version``, ``ranker_version``,
    ``data_revision``) it was computed from. The **current result set** is
    every ``JobMatch`` row with ``result_generation == current_generation``.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="match_result_state",
    )
    issued_generation = models.PositiveBigIntegerField(default=0)
    current_generation = models.PositiveBigIntegerField(null=True, blank=True)
    preference_revision = models.PositiveBigIntegerField(null=True, blank=True)
    preference_version = models.PositiveIntegerField(null=True, blank=True)
    ranker_version = models.CharField(max_length=32, blank=True, default="")
    data_revision = models.PositiveBigIntegerField(null=True, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    result_count = models.PositiveIntegerField(default=0)

    class Meta:
        app_label = "crank"

    def __str__(self):
        return f"match result state for {self.user_id} (gen={self.current_generation})"


__all__ = ["JobMatch", "MatchResultState"]
