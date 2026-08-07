# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Source catalog, per-source run records, and admin audit (issue #311).

Database-configured, operator-controlled source policy (approval/enabled/
limits/cadence/capabilities) linked to a rating ``Organization``, per-source
run/result records that let one failure never erase successful results, and a
credentials-safe admin audit trail. Adapter *implementations* live in code
(``crank.agents.sources``); these models hold only policy and provenance.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q, UniqueConstraint
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from crank.agents.sources.base import UnapprovedBaseUrl, validate_source_base_url
from crank.models.agent_run import AgentRun
from crank.models.organization import Organization
from crank.models.score import ScoreType


class ApprovalState(models.TextChoices):
    PENDING = "pending", _("Pending")
    APPROVED = "approved", _("Approved")
    BLOCKED = "blocked", _("Blocked")


class SourceCadence(models.TextChoices):
    HOURLY = "hourly", _("Hourly")
    DAILY = "daily", _("Daily")
    WEEKLY = "weekly", _("Weekly")
    MONTHLY = "monthly", _("Monthly")


class SourceCatalog(TimeStampedModel):
    """Operator-controlled policy for one external rating source.

    A source is only instantiable when it is ``approved``, ``enabled``, has an
    ``adapter_key`` registered in code, and its ``base_url`` is on the
    code-owned allowlist. This model deliberately stores no credentials or
    secret material.
    """

    name = models.CharField(max_length=100, unique=True, verbose_name=_("name"))
    organization = models.OneToOneField(
        Organization,
        on_delete=models.RESTRICT,
        related_name="source_catalog",
        limit_choices_to={"status": 1, "gives_ratings": True},
        verbose_name=_("rating organization"),
    )
    adapter_key = models.CharField(
        max_length=64,
        db_index=True,
        verbose_name=_("adapter key"),
        help_text=_("Identifier of the code-registered adapter implementation."),
    )
    base_url = models.URLField(
        max_length=512,
        verbose_name=_("base URL"),
        help_text=_(
            "HTTPS base URL on a code-owned allowlisted domain (SSRF guard)."
        ),
    )
    approval_state = models.CharField(
        max_length=16,
        choices=ApprovalState.choices,
        default=ApprovalState.PENDING,
        db_index=True,
        verbose_name=_("approval state"),
    )
    enabled = models.BooleanField(default=False, verbose_name=_("enabled"))
    approved_at = models.DateTimeField(null=True, blank=True)
    cadence = models.CharField(
        max_length=16,
        choices=SourceCadence.choices,
        default=SourceCadence.DAILY,
        verbose_name=_("request cadence"),
    )
    timeout_seconds = models.PositiveIntegerField(default=30)
    rate_limit_per_minute = models.PositiveIntegerField(default=60)
    max_response_bytes = models.PositiveIntegerField(default=1048576)
    terms_reviewed = models.BooleanField(default=False)
    terms_reviewed_at = models.DateTimeField(null=True, blank=True)
    supported_score_types = models.ManyToManyField(
        ScoreType,
        blank=True,
        related_name="sources",
        verbose_name=_("supported score types"),
    )

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        """Validate the base URL against the code-owned allowlist."""
        super().clean()
        if self.base_url:
            try:
                validate_source_base_url(self.base_url)
            except UnapprovedBaseUrl as exc:
                from django.core.exceptions import ValidationError as DjValidationError

                raise DjValidationError({"base_url": str(exc)}) from exc

    def supports_score_type(self, key: str) -> bool:
        """Return True when ``key`` names one of this source's capabilities."""
        return self.supported_score_types.filter(name=key).exists()

    # -- run/result conveniences (acceptance: answer last success/failure/
    # -- duration, and sanitized counts).
    def last_run(self):
        return self.runs.order_by("-started_at").first()

    def last_success_run(self):
        return (
            self.runs.filter(status=AgentRun.Status.SUCCEEDED)
            .order_by("-finished_at")
            .first()
        )

    def last_failure_run(self):
        return (
            self.runs.filter(status=AgentRun.Status.FAILED)
            .order_by("-finished_at")
            .first()
        )

    @property
    def last_success_at(self):
        run = self.last_success_run()
        return run.finished_at if run else None

    @property
    def last_failure_at(self):
        run = self.last_failure_run()
        return run.finished_at if run else None

    @property
    def last_run_duration(self) -> timedelta | None:
        run = self.last_run()
        return run.duration if run else None

    @property
    def last_run_counts(self) -> dict:
        run = self.last_run()
        return dict(run.counts) if run else {}

    class Meta:
        app_label = "crank"
        verbose_name = _("source catalog")
        verbose_name_plural = _("source catalog")
        indexes = [
            models.Index(fields=["adapter_key"], name="crank_source_adptkey_idx"),
            models.Index(fields=["approval_state"], name="crank_source_approval_idx"),
        ]


class SourceRun(TimeStampedModel):
    """One per-source run/result record.

    Each source run is its own row, so a later failed run never overwrites the
    sanitized counts/result of an earlier successful one. For correlation with
    the generic orchestrating ``AgentRun`` (scheduled execution), a nullable
    foreign key links to it without duplicating raw external data.
    """

    source = models.ForeignKey(
        SourceCatalog,
        on_delete=models.RESTRICT,
        related_name="runs",
        verbose_name=_("source"),
    )
    agent_run = models.ForeignKey(
        AgentRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("agent run"),
    )
    status = models.CharField(
        max_length=16,
        choices=AgentRun.Status.choices,
        default=AgentRun.Status.PENDING,
        db_index=True,
        verbose_name=_("status"),
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    adapter_version = models.CharField(max_length=32, blank=True, default="")
    counts = models.JSONField(default=dict, blank=True, verbose_name=_("counts"))
    error_summary = models.TextField(blank=True, default="", verbose_name=_("error summary"))

    def __str__(self) -> str:
        return f"{self.source.name} [{self.status}]"

    @property
    def duration(self) -> timedelta | None:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None

    def finalize(self, status, *, counts=None, error_summary="", finished_at=None):
        """Transition to a valid next status, updating timestamps/counts.

        Mirrors ``AgentRun.finalize``: ``running`` -> ``succeeded``/``failed``
        and ``pending`` -> ``running``/terminal. Terminal states reject further
        transitions.
        """
        _ALLOWED = {
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
        if status not in _ALLOWED.get(self.status, set()):
            raise ValueError(
                f"Invalid SourceRun transition {self.status} -> {status}"
            )
        self.status = status
        if status in (
            AgentRun.Status.SUCCEEDED,
            AgentRun.Status.FAILED,
            AgentRun.Status.SKIPPED,
        ):
            self.finished_at = finished_at or timezone.now()
        if counts is not None:
            self.counts = counts
        if error_summary:
            self.error_summary = error_summary
        self.save(
            update_fields=[
                "status",
                "finished_at",
                "counts",
                "error_summary",
            ]
        )

    class Meta:
        app_label = "crank"
        verbose_name = _("source run")
        verbose_name_plural = _("source runs")
        constraints = [
            UniqueConstraint(
                name="unique_source_run_running_per_source",
                fields=["source", "status"],
                condition=Q(status="running"),
                violation_error_message=(
                    "A source run for this source is already running."
                ),
            )
        ]


class SourceCatalogAudit(TimeStampedModel):
    """Credentials-safe audit trail for source-catalog admin changes.

    Stores only metadata (who, when, which action, changed field deltas) and
    never credentials or raw external data. Anything resembling a secret field
    name is redacted before it is recorded.
    """

    _SECRET_FIELD_SUBSTRINGS = ("key", "token", "secret", "password", "credential")

    class Action(models.TextChoices):
        CREATED = "created", "Created"
        CHANGED = "changed", "Changed"
        APPROVED = "approved", "Approved"
        BLOCKED = "blocked", "Blocked"
        ENABLED = "enabled", "Enabled"
        DISABLED = "disabled", "Disabled"

    source = models.ForeignKey(
        SourceCatalog,
        on_delete=models.CASCADE,
        related_name="audits",
        verbose_name=_("source"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_catalog_audits",
        verbose_name=_("actor"),
    )
    action = models.CharField(max_length=16, choices=Action.choices, db_index=True)
    changed_fields = models.JSONField(default=dict, blank=True)
    note = models.TextField(blank=True, default="")

    def __str__(self) -> str:
        return f"{self.action}:{self.source.name}"

    @staticmethod
    def _redact(value) -> str | bool | int | float | None:
        if value is None:
            return None
        if isinstance(value, (bool, int, float)):
            return value
        return str(value)[:200]

    @classmethod
    def record(
        cls, *, source, user, action, changes: dict | None = None, note: str = ""
    ) -> "SourceCatalogAudit":
        """Persist an audit row, redacting anything that looks secret."""
        safe_changes = {}
        for field, (old, new) in (changes or {}).items():
            lower = field.lower()
            if any(s in lower for s in cls._SECRET_FIELD_SUBSTRINGS):
                safe_changes[field] = {"from": "<redacted>", "to": "<redacted>"}
            else:
                safe_changes[field] = {"from": cls._redact(old), "to": cls._redact(new)}
        return cls.objects.create(
            source=source,
            user=user if user and user.is_authenticated else None,
            action=action,
            changed_fields=safe_changes,
            note=note[:500],
        )

    class Meta:
        app_label = "crank"
        verbose_name = _("source catalog audit")
        verbose_name_plural = _("source catalog audits")
