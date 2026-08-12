# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""User-submitted company suggestions and their bounded review workflow."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from crank.models.organization import Organization


_NAME_WHITESPACE = re.compile(r"\s+")
_URL_VALIDATOR = URLValidator(schemes=["https"])


def normalize_company_name(value: str) -> str:
    """Return a stable, case-insensitive identity key for a company name."""
    value = unicodedata.normalize("NFKC", value or "")
    return _NAME_WHITESPACE.sub(" ", value).strip().casefold()


def _unsafe_hostname(hostname: str) -> bool:
    """Reject hosts that are not safe public web destinations.

    Suggestions are never fetched by this workflow, but rejecting private and
    local names at validation time keeps the stored crawl input safe for the
    later source workflow too. DNS is intentionally not resolved here: model
    validation must not make an untrusted network request.
    """
    host = (hostname or "").rstrip(".").casefold()
    if not host or host in {"localhost", "localhost.localdomain", "local"}:
        return True
    if host.endswith((".localhost", ".local", ".internal", ".lan")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def normalize_public_url(value: str, *, required: bool = True) -> str:
    """Validate and canonicalize an HTTPS URL without allowing local hosts."""
    value = (value or "").strip()
    if not value:
        if required:
            raise ValidationError("A public HTTPS URL is required.")
        return ""
    try:
        _URL_VALIDATOR(value)
    except ValidationError as exc:
        raise ValidationError("Use a valid HTTPS URL.") from exc
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("Use a public HTTPS URL.") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in (None, 443)
        or _unsafe_hostname(parsed.hostname)
    ):
        raise ValidationError("Use a public HTTPS URL.")
    hostname = parsed.hostname.rstrip(".").casefold()
    netloc = hostname if port is None else f"{hostname}:443"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def normalize_domain(value: str) -> str:
    """Extract the registrable-input hostname key used for duplicate checks."""
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    return hostname.removeprefix("www.")


class CompanyRequest(TimeStampedModel):
    """A user suggestion awaiting staff identity and source review."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        DUPLICATE = "duplicate", _("Duplicate")

    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="company_requests",
    )
    company_name = models.CharField(max_length=100)
    normalized_name = models.CharField(max_length=100, db_index=True, editable=False)
    website_url = models.URLField(max_length=200)
    normalized_domain = models.CharField(max_length=255, db_index=True, editable=False)
    careers_url = models.URLField(max_length=200, blank=True, default="")
    reason = models.CharField(max_length=500, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    duplicate_of = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="duplicate_company_requests",
    )
    approved_organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_company_requests",
    )
    admin_note = models.CharField(max_length=500, blank=True, default="")
    crawl_source_approved = models.BooleanField(default=False)
    refresh_queued = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"{self.company_name} ({self.status})"

    def clean(self) -> None:
        super().clean()
        errors = {}
        self.company_name = (self.company_name or "").strip()
        self.reason = (self.reason or "").strip()
        self.admin_note = (self.admin_note or "").strip()
        self.normalized_name = normalize_company_name(self.company_name)
        if not self.normalized_name:
            errors["company_name"] = "Enter a company name."
        try:
            self.website_url = normalize_public_url(self.website_url)
            self.normalized_domain = normalize_domain(self.website_url)
        except ValidationError as exc:
            errors["website_url"] = exc.messages
        if self.careers_url:
            try:
                self.careers_url = normalize_public_url(self.careers_url, required=False)
            except ValidationError as exc:
                errors["careers_url"] = exc.messages
        if len(self.reason) > 500:
            errors["reason"] = "Keep the reason to 500 characters or fewer."
        if len(self.admin_note) > 500:
            errors["admin_note"] = "Keep the moderator note to 500 characters or fewer."
        if self.status == self.Status.DUPLICATE and not self.duplicate_of_id:
            errors["duplicate_of"] = "Choose the existing organization this duplicates."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean(validate_unique=False)
        return super().save(*args, **kwargs)

    @classmethod
    def find_existing_organization(cls, *, normalized_name, normalized_domain):
        """Find an active organization matching the submitted identity."""
        for organization in Organization.objects.filter(status=1):
            if normalize_company_name(organization.name) == normalized_name:
                return organization
            if organization.url and normalize_domain(organization.url) == normalized_domain:
                return organization
        return None

    class Meta:
        app_label = "crank"
        ordering = ["-created"]


__all__ = ["CompanyRequest", "normalize_company_name", "normalize_domain", "normalize_public_url"]
