# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Persisted summary of a single scheduled agent run.

A run claims the scheduler slot for its ``run_type`` through the partial
unique constraint below: at most one row may be ``running`` **or** ``pending``
per run type at a time. Overlapping invocations fail that insert and are
recorded as ``skipped`` instead. This is the database-backed overlap guard that
the Kubernetes scheduler's ``concurrencyPolicy: Forbid`` cannot fully replace
(retries, manual triggers, or multiple replicas can still collide).
"""
import uuid

from django.db import models
from django.db.models import Q, UniqueConstraint
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel


class AgentRun(TimeStampedModel):
    """One scheduled agent run and its outcome summary.

    Stores run type, status lifecycle, timestamps, a correlation id for tracing
    across logs and New Relic events, generic outcome counters, and a sanitized
    (secret-free, bounded) error summary. It deliberately does **not** store raw
    credentials, requests, or untrusted external data.
    """

    class RunType(models.TextChoices):
        NOOP = "noop", _("No-op reference run")
        GATHER_SCORES = "gather_scores", _("Score gathering run")
        JOB_PIPELINE = "job_pipeline", _("Job pipeline run")
        CRAWL_SCHEDULE = "crawl_schedule", _("Crawl scheduling run")
        CRAWL = "crawl", _("On-demand crawl run")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        RUNNING = "running", _("Running")
        SUCCEEDED = "succeeded", _("Succeeded")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped (another run is active)")

    run_type = models.CharField(
        max_length=32,
        choices=RunType.choices,
        db_index=True,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    # Shared across this run's logs and New Relic events for correlation.
    correlation_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Generic outcome counters; a command records the keys it observes
    # (e.g. items_seen, items_created, items_updated, items_failed).
    counts = models.JSONField(default=dict, blank=True)
    # Sanitized, bounded summary of a failure. Never contains raw credentials
    # or untrusted external/user data.
    error_summary = models.TextField(blank=True, default="")

    def __str__(self):
        return f"{self.run_type} [{self.status}]"

    def finalize(self, status, *, counts=None, error_summary=""):
        """Transition the run to its terminal status with timestamps/counters.

        Only allowed transitions are accepted (e.g. running -> succeeded/failed,
        pending -> running); invalid jumps (e.g. a terminal run back to running)
        raise ``ValueError``.
        """
        _ALLOWED_TRANSITIONS = {
            AgentRun.Status.PENDING: {
                AgentRun.Status.RUNNING,
                AgentRun.Status.SUCCEEDED,
                AgentRun.Status.FAILED,
                AgentRun.Status.SKIPPED,
            },
            AgentRun.Status.RUNNING: {
                AgentRun.Status.SUCCEEDED,
                AgentRun.Status.FAILED,
            },
            AgentRun.Status.SUCCEEDED: set(),
            AgentRun.Status.FAILED: set(),
            AgentRun.Status.SKIPPED: set(),
        }
        allowed = _ALLOWED_TRANSITIONS.get(self.status, set())
        if status not in allowed:
            raise ValueError(
                f"Invalid AgentRun transition {self.status} -> {status}"
            )
        self.status = status
        self.finished_at = timezone.now()
        if counts is not None:
            self.counts = counts
        self.error_summary = error_summary
        self.save(update_fields=["status", "finished_at", "counts", "error_summary"])

    class Meta:
        app_label = 'crank'
        constraints = [
            # At most one active run per run type. This is the database-backed
            # overlap lock covering both queued (PENDING) and in-flight
            # (RUNNING) runs: the first invocation to insert an active row
            # wins; later ones hit the constraint and are recorded as SKIPPED.
            # This is a strict superset of the historical running-only
            # constraint, so concurrent dispatchers that both see "no active
            # run" cannot both insert a PENDING row.
            UniqueConstraint(
                name="unique_agentrun_active_per_type",
                fields=["run_type", "status"],
                condition=Q(status__in=["running", "pending"]),
                violation_error_message=(
                    "An agent run of this type is already active (running or "
                    "pending); this invocation should be recorded as skipped."
                ),
            )
        ]
