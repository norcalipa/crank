# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Persisted, credentials-safe metadata for an operator-triggered crawl."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models
from django_extensions.db.models import TimeStampedModel

from crank.models.agent_run import AgentRun
from crank.models.job import JobSourceCatalog
from crank.models.source import SourceCatalog


class CrawlRun(TimeStampedModel):
    """One bounded crawl request and its sanitized outcome."""

    class SourceType(models.TextChoices):
        ORGANIZATION = "organization", "Organization"
        JOB = "job", "Job"

    class Outcome(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILURE = "failure", "Failure"
        PARTIAL = "partial", "Partial"
        TIMEOUT = "timeout", "Timeout"

    source_type = models.CharField(max_length=16, choices=SourceType.choices, db_index=True)
    source_key = models.CharField(max_length=64, db_index=True)
    source = models.ForeignKey(
        SourceCatalog,
        null=True,
        blank=True,
        on_delete=models.RESTRICT,
        related_name="crawl_runs",
    )
    job_source = models.ForeignKey(
        JobSourceCatalog,
        null=True,
        blank=True,
        on_delete=models.RESTRICT,
        related_name="crawl_runs",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="requested_crawl_runs",
    )
    agent_run = models.ForeignKey(
        AgentRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="crawl_runs",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(
        max_length=16,
        choices=Outcome.choices,
        default=Outcome.PENDING,
        db_index=True,
    )
    counts = models.JSONField(default=dict, blank=True)
    error_summary = models.TextField(blank=True, default="")

    class Meta:
        app_label = "crank"
        ordering = ["-started_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_type", "source_key", "outcome"],
                condition=models.Q(outcome="running"),
                name="unique_crawl_running_per_source",
            )
        ]
        indexes = [
            models.Index(fields=["source_type", "source_key", "started_at"], name="crank_crawl_source_time_idx"),
        ]

    def __str__(self):
        return f"{self.source_key} [{self.outcome}]"

    @property
    def duration(self) -> timedelta | None:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None


__all__ = ["CrawlRun"]
