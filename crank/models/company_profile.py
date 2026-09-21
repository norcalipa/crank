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


class CompanyFieldEvidence(TimeStampedModel):
    """The accepted claim about one organization field, with freshness state.

    Separate from :class:`CompanyProfileObservation` on purpose: an
    observation is a point-in-time reading of a whole page (and may be
    pending, rejected, or conflicted), while this row is the *decision* that
    one field, for one organization, is currently backed by an accepted
    observation. Exactly one row per ``(organization, field_key)`` may be in
    state ``accepted``; that invariant is serialized with
    ``select_for_update`` in ``crank.services.company_evidence`` because
    production MySQL cannot emit a partial unique constraint (W036).

    Like the observation model, this model intentionally contains no score
    fields: employment-policy evidence stays distinct from ratings.
    """

    class FieldKey(models.TextChoices):
        RTO_POLICY = "rto_policy", _("RTO policy")
        FUNDING_ROUND = "funding_round", _("Funding round")
        PUBLIC_STATUS = "public_status", _("Public status")
        ACCELERATED_VESTING = "accelerated_vesting", _("Accelerated vesting")
        LOCATIONS = "locations", _("Locations")
        COMPANY_NAME = "company_name", _("Company name")
        COMPANY_DOMAIN = "company_domain", _("Company domain")

    class State(models.TextChoices):
        ACCEPTED = "accepted", _("Accepted")
        SUPERSEDED = "superseded", _("Superseded")
        CONFLICTED = "conflicted", _("Conflicted")

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="field_evidence",
    )
    field_key = models.CharField(max_length=32, choices=FieldKey.choices, db_index=True)
    value_text = models.CharField(max_length=500, blank=True, default="")
    source_url = models.URLField(max_length=750)
    source_domain = models.CharField(max_length=253, blank=True, default="", db_index=True)
    observation = models.ForeignKey(
        CompanyProfileObservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="field_evidence",
    )
    scope_json = models.JSONField(default=dict, blank=True)
    observed_at = models.DateTimeField(db_index=True)
    validation_version = models.CharField(max_length=64)
    extractor_version = models.CharField(max_length=64)
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.ACCEPTED,
        db_index=True,
    )
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_successful_fetch_at = models.DateTimeField(null=True, blank=True)
    last_changed_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "crank"
        ordering = ["-observed_at", "-id"]
        indexes = [
            models.Index(
                fields=["organization", "field_key", "state"],
                name="crank_cfe_org_field_state_idx",
            ),
        ]
        # No partial unique constraint on (organization, field_key) where
        # state='accepted': production MySQL does not support them. The
        # one-accepted-row-per-field invariant is serialized by
        # select_for_update in crank.services.company_evidence.

    def __str__(self) -> str:
        return f"{self.organization_id}:{self.field_key} [{self.state}]"


__all__ = ["CompanyProfileObservation", "CompanyFieldEvidence"]
