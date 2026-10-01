# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""User-proposed corrections to one accepted company fact, held for staff review."""

from __future__ import annotations

import ipaddress
import unicodedata
from urllib.parse import urlsplit, urlunsplit

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
MAX_EVIDENCE_URL_LENGTH = 750
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def find_hidden_character(value: str, *, allow_newlines: bool = False) -> str | None:
    """First control, format (zero-width, bidi), surrogate or unassigned character, if any."""
    for char in value or "":
        if allow_newlines and char in "\n\t\r":
            continue
        if unicodedata.category(char) in _HIDDEN_CATEGORIES:
            return char
    return None


def strip_hidden_characters(value: str) -> str:
    """``value`` with every hidden character removed (comparison only, never storage)."""
    return "".join(c for c in value or "" if unicodedata.category(c) not in _HIDDEN_CATEGORIES)


def canonical_text(value: str) -> str:
    """NFKC, hidden characters removed, whitespace folded, case folded."""
    text = strip_hidden_characters(unicodedata.normalize("NFKC", value or ""))
    return " ".join(text.split()).casefold()


def _label_scripts(label: str) -> set[str]:
    scripts = set()
    for char in label:
        if char.isalpha():
            scripts.add(unicodedata.name(char, "UNKNOWN").split()[0])
    return scripts


def normalize_evidence_url(value: str) -> str:
    """Public HTTPS URL with an IDNA (punycode) host, free of hidden characters.

    Rejects hidden characters anywhere in the URL, IP-literal hosts, and host
    labels that mix scripts (look-alike hosts). The stored host is the ASCII
    punycode form, so what staff and users see is what resolves.
    """
    text = (value or "").strip()
    if find_hidden_character(text) is not None:
        raise ValidationError("The link contains hidden or control characters.")
    text = unicodedata.normalize("NFKC", text)
    try:
        raw_host = urlsplit(text).netloc.rpartition("@")[2]
    except ValueError as exc:
        raise ValidationError("Use a valid HTTPS URL.") from exc
    if raw_host.startswith("["):
        raise ValidationError("Use a link with a website name, not an IP address.")
    normalized = normalize_public_url(text)
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValidationError("Use a link with a website name, not an IP address.")
    if any(len(_label_scripts(label)) > 1 for label in host.split(".")):
        raise ValidationError("The link's website name mixes alphabets; use its plain form.")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationError("Use a valid HTTPS URL.") from exc
    netloc = ascii_host if parts.port is None else f"{ascii_host}:{parts.port}"
    result = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
    if len(result) > MAX_EVIDENCE_URL_LENGTH:
        raise ValidationError("Use a link of 750 characters or fewer.")
    return result


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
        hidden = "Remove hidden or control characters (zero-width, direction or other invisible marks)."
        for name in ("proposed_value", "scope_value", "note"):
            raw = getattr(self, name) or ""
            if find_hidden_character(raw, allow_newlines=name == "note") is not None:
                errors[name] = hidden
        self.proposed_value = " ".join(unicodedata.normalize("NFKC", self.proposed_value or "").split())
        self.scope_value = " ".join(unicodedata.normalize("NFKC", self.scope_value or "").split())
        self.note = unicodedata.normalize("NFKC", self.note or "").strip()
        if "proposed_value" in errors:
            pass
        elif not self.proposed_value:
            errors["proposed_value"] = "Enter the corrected value."
        elif len(self.proposed_value) > MAX_VALUE_LENGTH:
            errors["proposed_value"] = "Keep the corrected value to 500 characters or fewer."
        try:
            self.evidence_url = normalize_evidence_url(self.evidence_url)
        except ValidationError as exc:
            errors["evidence_url"] = exc.messages
        if self.scope_level == self.ScopeLevel.COMPANY:
            if self.scope_value:
                errors["scope_value"] = "Leave the scope detail empty for a company-wide correction."
        elif "scope_value" in errors:
            pass
        elif not self.scope_value:
            errors["scope_value"] = "Say which team, role or location this applies to."
        elif len(self.scope_value) > MAX_SCOPE_VALUE_LENGTH:
            errors["scope_value"] = "Keep the scope detail to 100 characters or fewer."
        if len(self.note) > 500:
            errors["note"] = "Keep the note to 500 characters or fewer."
        if errors:
            raise ValidationError(errors)


__all__ = [
    "CompanyCorrection",
    "canonical_text",
    "find_hidden_character",
    "normalize_evidence_url",
    "strip_hidden_characters",
]
