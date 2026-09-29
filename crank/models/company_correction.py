# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""User-proposed corrections to one accepted company fact, held for staff review."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from crank.models.company_profile import CompanyFieldEvidence
from crank.models.company_request import normalize_public_url
from crank.models.organization import Organization

MAX_VALUE_LENGTH = 500
MAX_SCOPE_VALUE_LENGTH = 100


class CompanyCorrection(TimeStampedModel):
    """A pending proposal that never changes accepted facts until reviewed."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending review")
        ACCEPTED = "accepted", _("Accepted")
        REJECTED = "rejected", _("Rejected")
        DUPLICATE = "duplicate", _("Duplicate")

    class ScopeLevel(models.TextChoices):
        COMPANY = "company", _("Company")
        TEAM = "team", _("Team")
        ROLE = "role", _("Role")
        LOCATION = "location", _("Location")

    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="company_corrections",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="corrections",
    )
    field_key = models.CharField(max_length=32, choices=CompanyFieldEvidence.FieldKey.choices)
    current_value = models.CharField(max_length=MAX_VALUE_LENGTH, blank=True, default="")
    current_evidence = models.ForeignKey(
        CompanyFieldEvidence,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="corrections",
    )
    proposed_value = models.CharField(max_length=MAX_VALUE_LENGTH)
    evidence_url = models.URLField(max_length=750)
    scope_level = models.CharField(
        max_length=16, choices=ScopeLevel.choices, default=ScopeLevel.COMPANY
    )
    scope_value = models.CharField(max_length=MAX_SCOPE_VALUE_LENGTH, blank=True, default="")
    note = models.CharField(max_length=500, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    admin_note = models.CharField(max_length=500, blank=True, default="")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_company_corrections",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.UUIDField()

    class Meta:
        app_label = "crank"
        ordering = ["-created", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["requester", "idempotency_key"],
                name="unique_company_correction_idempotency",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "field_key", "status"],
                name="crank_cc_org_field_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization_id}:{self.field_key} [{self.status}]"

    def clean(self) -> None:
        super().clean()
        errors = {}
        self.proposed_value = " ".join((self.proposed_value or "").split())
        self.scope_value = " ".join((self.scope_value or "").split())
        self.note = (self.note or "").strip()
        if not self.proposed_value:
            errors["proposed_value"] = "Enter the corrected value."
        elif len(self.proposed_value) > MAX_VALUE_LENGTH:
            errors["proposed_value"] = "Keep the corrected value to 500 characters or fewer."
        try:
            self.evidence_url = normalize_public_url(self.evidence_url)
        except ValidationError as exc:
            errors["evidence_url"] = exc.messages
        if self.scope_level == self.ScopeLevel.COMPANY:
            if self.scope_value:
                errors["scope_value"] = "Leave the scope detail empty for a company-wide correction."
        elif not self.scope_value:
            errors["scope_value"] = "Say which team, role or location this applies to."
        elif len(self.scope_value) > MAX_SCOPE_VALUE_LENGTH:
            errors["scope_value"] = "Keep the scope detail to 100 characters or fewer."
        if len(self.note) > 500:
            errors["note"] = "Keep the note to 500 characters or fewer."
        if errors:
            raise ValidationError(errors)


__all__ = ["CompanyCorrection"]
