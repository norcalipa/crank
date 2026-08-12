# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Crawled company facts kept separate from reviewed organization policy."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django_extensions.db.models import TimeStampedModel
from django.utils.translation import gettext_lazy as _

from crank.models.organization import Organization


class CompanyProfileObservation(TimeStampedModel):
    """One bounded, provenance-bearing observation from a company crawl.

    This model intentionally does not contain score fields.  A crawl can be
    accepted for identity and profile metadata without changing any rating or
    policy value on :class:`Organization`.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending review")
        AUTO_APPLIED = "auto_applied", _("Auto-applied")
        ACCEPTED = "accepted", _("Accepted")
        REJECTED = "rejected", _("Rejected")
        CONFLICTED = "conflicted", _("Conflicted")

    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="company_profile_observations",
    )
    source_url = models.URLField(max_length=750)
    observed_domain = models.CharField(max_length=253, blank=True, default="", db_index=True)
    observed_name = models.CharField(max_length=200, blank=True, default="")
    description = models.TextField(blank=True, default="")
    locations = models.JSONField(default=list, blank=True)
    rto_evidence = models.TextField(blank=True, default="")
    funding_evidence = models.TextField(blank=True, default="")
    public_status_evidence = models.TextField(blank=True, default="")
    logo_url = models.URLField(max_length=750, blank=True, default="")
    brand_metadata = models.JSONField(default=dict, blank=True)
    observed_at = models.DateTimeField(db_index=True)
    extraction_version = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    conflict_fields = models.JSONField(default=list, blank=True)
    admin_note = models.TextField(blank=True, default="")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_company_profile_observations",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    fingerprint = models.CharField(max_length=64, blank=True, default="", db_index=True)

    class Meta:
        app_label = "crank"
        ordering = ["-observed_at", "-id"]
        indexes = [
            models.Index(fields=["observed_domain", "status"], name="crank_cpo_domain_status_idx"),
            models.Index(fields=["source_url", "observed_at"], name="crank_cpo_source_time_idx"),
        ]

    def __str__(self) -> str:
        identity = self.observed_name or self.observed_domain or self.source_url
        return f"{identity} [{self.status}]"

    def mark_reviewed(self, *, status: str, user=None, note: str = "") -> None:
        """Set an operator review outcome without touching score data."""
        if status not in {
            self.Status.ACCEPTED,
            self.Status.REJECTED,
            self.Status.CONFLICTED,
        }:
            raise ValueError("invalid company profile review status")
        from django.utils import timezone

        self.status = status
        self.reviewed_by = user if user and user.is_authenticated else None
        self.reviewed_at = timezone.now()
        if note:
            self.admin_note = note[:500]
        self.save(update_fields=["status", "reviewed_by", "reviewed_at", "admin_note", "modified"])


__all__ = ["CompanyProfileObservation"]
