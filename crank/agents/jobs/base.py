# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Shared job-source adapter contract and boundary validation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from math import isfinite
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from django.utils import timezone
from django.utils.html import strip_tags

from crank.agents.jobs.errors import JobSchemaError, UnapprovedJobSource

# These are code-owned, reviewed hosts. The example host is intentionally
# retained for synthetic fixtures; database rows cannot expand this set.
APPROVED_JOB_SOURCE_DOMAINS = frozenset(
    {
        # Synthetic fixture host used by contract tests; real job-source
        # hosts are copied from the reviewed job-source catalog allowlist.
        "data.usajobs.gov",
        "developer.usajobs.gov",
        "www.opm.gov",
        "www.usajobs.gov",
        "remoteok.com",
        "news.ycombinator.com",
        "hackernews.firebaseio.com",
        "boards-api.greenhouse.io",
        "api.lever.co",
        "jobs.example.test",
    }
)
MAX_EXTERNAL_ID = 256
MAX_URL = 1024
MAX_EMPLOYER_NAME = 200
MAX_EMPLOYER_DOMAIN = 253
MAX_TITLE = 300
MAX_LOCATION = 500
MAX_CURRENCY = 3
MAX_INTERVAL = 32
MAX_DESCRIPTION_EXCERPT = 2000
MAX_METADATA_BYTES = 8192
MAX_CATALOG_METADATA_BYTES = 8192
COMPENSATION_MAX_DIGITS = 14
COMPENSATION_DECIMAL_PLACES = 2


def _clean_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise JobSchemaError(f"{field} must be a string")
    if len(value) > maximum:
        raise JobSchemaError(f"{field} exceeds {maximum} characters")
    return " ".join(strip_tags(value).split())


def _required_text(value: str, field: str, maximum: int) -> str:
    value = _clean_text(value, field, maximum)
    if not value:
        raise JobSchemaError(f"{field} must be non-empty")
    return value


def validate_job_url(value: str, *, allow_hosts: set[str] | frozenset[str] | None = None) -> str:
    """Return a normalized HTTPS URL on a code-owned host."""
    if not isinstance(value, str) or len(value) > MAX_URL:
        raise JobSchemaError("canonical_url is missing or too long")
    value = value.strip()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise JobSchemaError("canonical_url is malformed") from exc
    if parsed.scheme.lower() != "https" or not host or parsed.username or parsed.password:
        raise JobSchemaError("canonical_url must be an HTTPS URL without credentials")
    host = host.lower().rstrip(".")
    hosts = allow_hosts if allow_hosts is not None else APPROVED_JOB_SOURCE_DOMAINS
    if not any(host == allowed or host.endswith("." + allowed) for allowed in hosts):
        raise UnapprovedJobSource(f"job URL host {host!r} is not allowlisted")
    if port is not None:
        raise JobSchemaError("canonical_url must not include a port")
    return parsed._replace(fragment="").geturl()


def _validate_domain(value: str) -> str:
    value = _required_text(value, "employer_domain", MAX_EMPLOYER_DOMAIN).lower().rstrip(".")
    if "/" in value or ":" in value or "@" in value or "." not in value:
        raise JobSchemaError("employer_domain must be a hostname")
    return value


def _validate_date(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise JobSchemaError(f"{field} must be timezone-aware")
    return value


@dataclass(frozen=True)
class RawJobListing:
    """Validated, source-neutral listing returned by an adapter."""

    external_id: str
    canonical_url: str
    employer_name: str
    title: str
    location_text: str = ""
    is_remote: bool = False
    compensation_min: float | None = None
    compensation_max: float | None = None
    compensation_currency: str = ""
    compensation_interval: str = ""
    description_excerpt: str = ""
    employer_domain: str = ""
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    status: str = "active"
    source_metadata: Mapping[str, Any] | None = None
    employer_external_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "external_id", _clean_text(self.external_id, "external_id", MAX_EXTERNAL_ID))
        object.__setattr__(self, "canonical_url", validate_job_url(self.canonical_url))
        for field, maximum in (("employer_name", MAX_EMPLOYER_NAME), ("title", MAX_TITLE),
                               ("location_text", MAX_LOCATION), ("description_excerpt", MAX_DESCRIPTION_EXCERPT)):
            object.__setattr__(self, field, _clean_text(getattr(self, field), field, maximum))
        if not self.employer_name or not self.title:
            raise JobSchemaError("employer_name and title must be non-empty")
        object.__setattr__(self, "employer_external_id", _clean_text(
            self.employer_external_id, "employer_external_id", MAX_EXTERNAL_ID
        ))
        if self.employer_domain:
            object.__setattr__(self, "employer_domain", _validate_domain(self.employer_domain))
        if not isinstance(self.is_remote, bool):
            raise JobSchemaError("is_remote must be boolean")
        for field in ("compensation_min", "compensation_max"):
            value = getattr(self, field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float, Decimal))
                or not _is_finite_number(value)
                or value < 0
            ):
                raise JobSchemaError(f"{field} must be a finite non-negative number")
            if value is not None:
                object.__setattr__(self, field, _validate_compensation(value, field))
        if self.compensation_min is not None and self.compensation_max is not None and self.compensation_min > self.compensation_max:
            raise JobSchemaError("compensation_min must not exceed compensation_max")
        object.__setattr__(self, "compensation_currency", _clean_text(self.compensation_currency, "compensation_currency", MAX_CURRENCY).upper())
        object.__setattr__(self, "compensation_interval", _clean_text(self.compensation_interval, "compensation_interval", MAX_INTERVAL))
        if self.status not in {"active", "closed", "expired"}:
            raise JobSchemaError(f"unsupported listing status: {self.status!r}")
        first = _validate_date(self.first_seen_at, "first_seen_at")
        last = _validate_date(self.last_seen_at, "last_seen_at")
        if first is None and last is None:
            last = timezone.now()
            first = last
            object.__setattr__(self, "first_seen_at", first)
            object.__setattr__(self, "last_seen_at", last)
        elif first is None:
            first = last
            object.__setattr__(self, "first_seen_at", first)
        elif last is None:
            last = first
            object.__setattr__(self, "last_seen_at", last)
        if first and last and first > last:
            raise JobSchemaError("first_seen_at must not be after last_seen_at")
        object.__setattr__(
            self,
            "source_metadata",
            validate_source_metadata(self.source_metadata),
        )


def _is_finite_number(value: int | float | Decimal) -> bool:
    if isinstance(value, Decimal):
        return value.is_finite()
    return isfinite(value)


def _validate_compensation(value: int | float | Decimal, field: str) -> Decimal:
    """Normalize compensation to the persistence field's exact range."""
    try:
        amount = Decimal(str(value))
        quantum = Decimal(1).scaleb(-COMPENSATION_DECIMAL_PLACES)
        normalized = amount.quantize(quantum)
    except (InvalidOperation, ValueError) as exc:
        raise JobSchemaError(f"{field} does not fit the compensation field") from exc
    if normalized != amount:
        raise JobSchemaError(
            f"{field} must have no more than {COMPENSATION_DECIMAL_PLACES} decimal places"
        )
    if abs(normalized) >= Decimal(10) ** (
        COMPENSATION_MAX_DIGITS - COMPENSATION_DECIMAL_PLACES
    ):
        raise JobSchemaError(
            f"{field} exceeds the {COMPENSATION_MAX_DIGITS}-digit compensation range"
        )
    return normalized


def validate_source_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and copy metadata without secrets or raw response bodies."""
    try:
        metadata = dict(value or {})
    except (TypeError, ValueError) as exc:
        raise JobSchemaError("source_metadata must be a mapping") from exc
    _validate_metadata(metadata)
    try:
        if len(json.dumps(metadata, separators=(",", ":"))) > MAX_METADATA_BYTES:
            raise JobSchemaError(f"source_metadata exceeds {MAX_METADATA_BYTES} bytes")
    except (TypeError, ValueError) as exc:
        raise JobSchemaError("source_metadata must be JSON serializable") from exc
    return metadata


_CATALOG_SENSITIVE_FIELDS = (
    "authorization",
    "api_key",
    "password",
    "token",
    "secret",
    "credential",
    "raw_body",
    "response_body",
)


def validate_catalog_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Sanitize bounded catalog provenance; never store credentials/raw bodies."""
    try:
        metadata = _sanitize_catalog_metadata(dict(value or {}), "catalog_metadata")
        encoded = json.dumps(metadata, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise JobSchemaError("catalog_metadata must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_CATALOG_METADATA_BYTES:
        raise JobSchemaError(
            f"catalog_metadata exceeds {MAX_CATALOG_METADATA_BYTES} bytes"
        )
    return metadata


def _sanitize_catalog_metadata(value: Any, path: str) -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise JobSchemaError(f"{path} contains a non-string key")
            if any(field in key.lower() for field in _CATALOG_SENSITIVE_FIELDS):
                continue
            result[key] = _sanitize_catalog_metadata(item, f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_catalog_metadata(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, float) and not isfinite(value):
        raise JobSchemaError(f"{path} contains a non-finite number")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise JobSchemaError(f"{path} contains unsupported metadata")
    return value


def _validate_metadata(value: Any, path: str = "source_metadata") -> None:
    """Reject secrets, raw payloads, and unsupported metadata values."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            if any(secret in key_text for secret in ("password", "credential", "token", "secret", "raw_body", "response_body")):
                raise JobSchemaError(f"{path} contains prohibited field {key!r}")
            _validate_metadata(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_metadata(item, f"{path}[{index}]")
    elif isinstance(value, float) and not isfinite(value):
        raise JobSchemaError(f"{path} contains a non-finite number")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise JobSchemaError(f"{path} contains unsupported metadata")


@dataclass(frozen=True)
class JobSourceQuery:
    """Bounded source-neutral query passed to an adapter."""

    keyword: str = ""
    location: str = ""
    max_listings: int = 100
    max_pages: int = 10

    def __post_init__(self) -> None:
        _clean_text(self.keyword, "keyword", 200)
        _clean_text(self.location, "location", MAX_LOCATION)
        if not isinstance(self.max_listings, int) or not 1 <= self.max_listings <= 10000:
            raise JobSchemaError("max_listings must be between 1 and 10000")
        if not isinstance(self.max_pages, int) or not 1 <= self.max_pages <= 1000:
            raise JobSchemaError("max_pages must be between 1 and 1000")


@dataclass(frozen=True)
class JobSourceResult:
    listings: Sequence[RawJobListing]
    pages_fetched: int = 0
    items_seen: int = 0


class JobSourceAdapter(ABC):
    """Abstract contract implemented by source-specific adapters."""

    key: str
    version: str

    def __init__(self, source) -> None:
        self.source = source

    @abstractmethod
    def fetch(self, query: JobSourceQuery) -> JobSourceResult:
        """Fetch validated listings for ``query``."""
        raise NotImplementedError


__all__ = [
    "RawJobListing",
    "JobSourceResult",
    "JobSourceQuery",
    "JobSourceAdapter",
    "validate_job_url",
    "validate_source_metadata",
    "validate_catalog_metadata",
]
