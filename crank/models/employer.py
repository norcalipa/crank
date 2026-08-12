# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Curated employer identity mappings and deterministic job resolution."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from django.conf import settings
from django.db import models
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from crank.models.job import JobListing
from crank.models.organization import Organization

MAX_EMPLOYER_TEXT = 200
MAX_EMPLOYER_DOMAIN = 253
MAX_EMPLOYER_IDENTIFIER = 256
_MAX_PROVENANCE_BYTES = 2048


def sanitize_employer_text(value: Any, maximum: int = MAX_EMPLOYER_TEXT) -> str:
    """Bound and remove markup from source-controlled employer text."""
    if value is None:
        return ""
    value = strip_tags(str(value))
    value = unicodedata.normalize("NFKC", value)
    return " ".join(value.split())[:maximum]


def normalize_employer_name(value: Any) -> str:
    return sanitize_employer_text(value).casefold()


def normalize_employer_domain(value: Any) -> str:
    value = sanitize_employer_text(value, MAX_EMPLOYER_DOMAIN).casefold().rstrip(".")
    while value.startswith("www."):
        value = value[4:]
    return value


def normalize_employer_identifier(value: Any) -> str:
    return sanitize_employer_text(value, MAX_EMPLOYER_IDENTIFIER)


def _normalized_alias_value(kind: str, value: Any) -> str:
    if kind == EmployerAlias.AliasKind.DOMAIN:
        return normalize_employer_domain(value)
    if kind == EmployerAlias.AliasKind.NAME:
        return normalize_employer_name(value)
    return normalize_employer_identifier(value)


class EmployerAlias(TimeStampedModel):
    """A reviewed source identifier mapped to a known organization."""

    class AliasKind(models.TextChoices):
        EXTERNAL_ID = "external_id", _("External ID")
        DOMAIN = "domain", _("Domain")
        NAME = "name", _("Normalized Name")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="employer_aliases",
    )
    kind = models.CharField(max_length=16, choices=AliasKind.choices)
    value = models.CharField(max_length=253)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    provenance = models.JSONField(default=dict, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_employer_aliases",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "crank"
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "value"],
                name="unique_employer_alias_kind_value",
            )
        ]
        indexes = [
            models.Index(
                fields=["kind", "value", "status"],
                name="crank_empr_alias_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.kind}: {self.value}"

    def clean(self):
        super().clean()
        self.value = _normalized_alias_value(self.kind, self.value)
        if not isinstance(self.provenance, dict):
            self.provenance = {}
        # Provenance is operator metadata, not a place for unbounded input.
        self.provenance = {
            str(key)[:64]: sanitize_employer_text(value, 500)
            for key, value in self.provenance.items()
        }

    def save(self, *args, **kwargs):
        self.value = _normalized_alias_value(self.kind, self.value)
        if not isinstance(self.provenance, dict):
            self.provenance = {}
        self.provenance = {
            str(key)[:64]: sanitize_employer_text(value, 500)
            for key, value in self.provenance.items()
        }
        return super().save(*args, **kwargs)


class UnresolvedEmployer(TimeStampedModel):
    """A bounded, reason-coded employer outcome awaiting operator review."""

    class Reason(models.TextChoices):
        NO_MATCH = "no_match", _("No Match")
        AMBIGUOUS = "ambiguous", _("Ambiguous")
        INACTIVE = "inactive", _("Inactive Organization")
        NOT_PUBLIC = "not_public", _("Not Public Organization")

    listing = models.ForeignKey(
        JobListing,
        on_delete=models.CASCADE,
        related_name="unresolved_employers",
    )
    employer_name = models.CharField(max_length=200)
    employer_domain = models.CharField(max_length=253, blank=True, default="")
    reason = models.CharField(max_length=16, choices=Reason.choices)
    candidates = models.JSONField(default=dict, blank=True)
    resolved = models.BooleanField(default=False, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "crank"
        constraints = [
            models.UniqueConstraint(
                fields=["listing"],
                condition=models.Q(resolved=False),
                name="unique_open_unresolved_employer_listing",
            )
        ]
        indexes = [
            models.Index(fields=["reason", "resolved"], name="crank_unres_employer_idx")
        ]

    def __str__(self) -> str:
        return f"{self.listing_id}: {self.reason}"
