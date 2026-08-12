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
    first_matched_at = models.DateTimeField()
    last_matched_at = models.DateTimeField()
    seen_at = models.DateTimeField(null=True, blank=True)
    dismissed = models.BooleanField(default=False, db_index=True)

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
        ]

    def __str__(self):
        return f"{self.listing} for {self.user}"


__all__ = ["JobMatch"]
