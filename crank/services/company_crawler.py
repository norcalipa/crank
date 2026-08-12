# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Bounded, review-first company profile crawling."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.html import strip_tags

from crank.agents.jobs.base import APPROVED_JOB_SOURCE_DOMAINS, validate_job_url
from crank.agents.jobs.errors import JobSourceDisabled, JobSourceNotApproved
from crank.agents.jobs.firecrawl import FirecrawlClient
from crank.agents.sources.errors import BlockedRedirectError, SchemaDriftError
from crank.models.company_profile import CompanyProfileObservation
from crank.models.employer import EmployerAlias, normalize_employer_domain, normalize_employer_name
from crank.models.job import JobSourceCatalog
from crank.models.organization import Organization
from crank.services import monitoring

EXTRACTION_VERSION = "firecrawl-company-profile.v1"
MAX_ITEMS = 10
MAX_DESCRIPTION = 4000
MAX_EVIDENCE = 2000
MAX_LOCATIONS = 50
PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "career_url": {"type": "string", "format": "uri"},
        "company_domain": {"type": "string"},
        "company_name": {"type": "string"},
        "description": {"type": "string"},
        "locations": {"type": ["array", "string"]},
        "rto_evidence": {"type": "string"},
        "funding_evidence": {"type": "string"},
        "public_status_evidence": {"type": "string"},
        "logo_url": {"type": "string", "format": "uri"},
        "brand_metadata": {"type": "object"},
    },
}


@dataclass(frozen=True)
class CompanyCrawlResult:
    """Sanitized counters from one crawl; raw provider content is discarded."""

    observations: int = 0
    accepted: int = 0
    auto_applied: int = 0
    rejected: int = 0
    conflicted: int = 0
    pending: int = 0
    duplicates: int = 0
    errors: int = 0
    freshness_seconds: float | None = None
    error_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return self.observations


def _text(value: Any, field_name: str, maximum: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SchemaDriftError(f"{field_name} must be a string")
    value = " ".join(strip_tags(value).split())
    if len(value) > maximum:
        raise SchemaDriftError(f"{field_name} exceeds configured limit")
    if required and not value:
        raise SchemaDriftError(f"{field_name} must be non-empty")
    return value


def _url(value: Any, *, allowed_hosts: set[str] | frozenset[str], field_name: str) -> str:
    value = _text(value, field_name, 750, required=True)
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise BlockedRedirectError(f"{field_name} is malformed") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in allowed_hosts and not any(host.endswith("." + item) for item in allowed_hosts):
        raise BlockedRedirectError(f"{field_name} is outside the approved domain")
    return validate_job_url(value, allow_hosts=allowed_hosts)


def _domain(value: Any) -> str:
    value = _text(value, "company_domain", 253, required=True)
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if parsed.username or parsed.password or parsed.port or parsed.path not in ("", "/"):
        raise SchemaDriftError("company_domain must be a hostname")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or "." not in host or any(char.isspace() for char in host):
        raise SchemaDriftError("company_domain must be a hostname")
    return normalize_employer_domain(host)


def _locations(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    elif value is None:
        values = []
    else:
        raise SchemaDriftError("locations must be a list or string")
    if len(values) > MAX_LOCATIONS:
        raise SchemaDriftError("locations exceeds configured limit")
    return [_text(item, "location", 300, required=True) for item in values]


def _brand(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SchemaDriftError("brand_metadata must be an object")
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:20]:
        if not isinstance(key, str) or not isinstance(item, (str, int, float, bool)):
            raise SchemaDriftError("brand_metadata contains unsupported data")
        result[key[:64]] = str(item)[:256] if isinstance(item, str) else item
    return result


def _safe_reason(exc: Exception) -> str:
    return f"{type(exc).__name__} ({monitoring.failure_reason(exc)})"


def _source_host(source: Any) -> str:
    try:
        parsed = urlsplit(str(source.base_url))
        host = (parsed.hostname or "").lower().rstrip(".")
        validate_job_url(str(source.base_url), allow_hosts=APPROVED_JOB_SOURCE_DOMAINS)
    except Exception as exc:
        raise BlockedRedirectError("company crawl source URL is not approved") from exc
    if host not in APPROVED_JOB_SOURCE_DOMAINS:
        raise BlockedRedirectError("company crawl source domain is not code-approved")
    return host


def _organization_for(domain: str, name: str) -> tuple[Organization | None, list[str]]:
    """Resolve only deterministic identities, including reviewed aliases."""
    domain_matches = set()
    name_matches = set()
    organizations = Organization.objects.all().only("id", "name", "url")
    domain_key = normalize_employer_domain(domain)
    name_key = normalize_employer_name(name)
    for organization in organizations:
        if normalize_employer_domain(urlsplit(organization.url).hostname or "") == domain_key:
            domain_matches.add(organization.pk)
        if normalize_employer_name(organization.name) == name_key:
            name_matches.add(organization.pk)
    domain_matches.update(
        EmployerAlias.objects.filter(
            kind=EmployerAlias.AliasKind.DOMAIN,
            value=domain_key,
            status=EmployerAlias.Status.APPROVED,
        ).values_list("organization_id", flat=True)
    )
    name_matches.update(
        EmployerAlias.objects.filter(
            kind=EmployerAlias.AliasKind.NAME,
            value=name_key,
            status=EmployerAlias.Status.APPROVED,
        ).values_list("organization_id", flat=True)
    )
    if len(domain_matches) > 1 or len(name_matches) > 1:
        return None, ["organization_identity"]
    if domain_matches and name_matches and domain_matches != name_matches:
        return None, ["organization_identity"]
    organization_id = next(iter(domain_matches or name_matches), None)
    organization = Organization.objects.filter(pk=organization_id).first() if organization_id else None
    return organization, []


def _fingerprint(data: Mapping[str, Any]) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _extract(item: Any, source_host: str, source_url: str) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise SchemaDriftError("profile item must be an object")
    data = item.get("extract", item.get("json", item))
    if not isinstance(data, Mapping):
        raise SchemaDriftError("profile extraction must be an object")
    metadata = item.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise SchemaDriftError("profile metadata must be an object")
    career_url = data.get("career_url") or metadata.get("sourceURL") or source_url
    observed_url = _url(career_url, allowed_hosts={source_host}, field_name="career_url")
    domain = _domain(data.get("company_domain") or source_host)
    name = _text(data.get("company_name"), "company_name", 200, required=True)
    logo = data.get("logo_url", "")
    if logo:
        logo = _url(logo, allowed_hosts={source_host, domain}, field_name="logo_url")
    return {
        "source_url": observed_url,
        "observed_domain": domain,
        "observed_name": name,
        "description": _text(data.get("description", ""), "description", MAX_DESCRIPTION),
        "locations": _locations(data.get("locations", [])),
        "rto_evidence": _text(data.get("rto_evidence", ""), "rto_evidence", MAX_EVIDENCE),
        "funding_evidence": _text(data.get("funding_evidence", ""), "funding_evidence", MAX_EVIDENCE),
        "public_status_evidence": _text(data.get("public_status_evidence", ""), "public_status_evidence", MAX_EVIDENCE),
        "logo_url": logo,
        "brand_metadata": _brand(data.get("brand_metadata", {})),
    }


def _client(client: Any | None) -> Any:
    if client is not None:
        return client
    return FirecrawlClient(
        base_url=str(getattr(settings, "FIRECRAWL_BASE_URL", "https://api.firecrawl.dev")),
        api_key=str(getattr(settings, "FIRECRAWL_API_KEY", "")),
        timeout=float(getattr(settings, "FIRECRAWL_TIMEOUT", 30.0)),
        max_bytes=int(getattr(settings, "JOBS_ADAPTER_MAX_BYTES", 2 * 1024 * 1024)),
    )


def crawl_company_profile(source: Any, *, client: Any | None = None, now: datetime | None = None) -> CompanyCrawlResult:
    """Crawl at most the configured page budget and persist reviewable facts."""
    if isinstance(source, JobSourceCatalog):
        if source.approval_state != JobSourceCatalog.ApprovalState.APPROVED:
            raise JobSourceNotApproved("company crawl source is not approved")
        if not source.enabled:
            raise JobSourceDisabled("company crawl source is disabled")
        if source.adapter_key.lower() != "firecrawl-careers":
            raise BlockedRedirectError("source is not cataloged for Firecrawl")
    host = _source_host(source)
    source_url = validate_job_url(str(source.base_url), allow_hosts={host})
    observed_at = now or timezone.now()
    try:
        crawl_client = _client(client)
        response = crawl_client.crawl_url(
            source_url,
            max_pages=min(int(getattr(settings, "FIRECRAWL_MAX_PAGES", 10)), MAX_ITEMS),
            max_listings=MAX_ITEMS,
            credit_budget=int(getattr(settings, "FIRECRAWL_CREDIT_BUDGET", 10)),
            extraction_schema=PROFILE_SCHEMA,
        )
        items = response.get("data", [])
        if not isinstance(items, list):
            raise SchemaDriftError("Firecrawl profile data must be a list")
    except Exception as exc:
        return CompanyCrawlResult(errors=1, error_reasons=(_safe_reason(exc),))

    counts = {"observations": 0, "accepted": 0, "auto_applied": 0, "rejected": 0,
              "conflicted": 0, "pending": 0, "duplicates": 0, "errors": 0}
    reasons: list[str] = []
    for item in items[:MAX_ITEMS]:
        try:
            data = _extract(item, host, source_url)
            organization, identity_conflicts = _organization_for(data["observed_domain"], data["observed_name"])
            fp = _fingerprint(data)
            if CompanyProfileObservation.objects.filter(fingerprint=fp).exists():
                counts["duplicates"] += 1
                continue
            prior = (CompanyProfileObservation.objects.filter(organization=organization)
                     .exclude(status=CompanyProfileObservation.Status.REJECTED)
                     .order_by("-observed_at").first()) if organization else None
            conflict_fields = list(identity_conflicts)
            if prior:
                if prior.observed_at > observed_at:
                    conflict_fields.append("stale_observation")
                for field in ("source_url", "observed_domain", "observed_name", "description", "locations",
                              "rto_evidence", "funding_evidence", "public_status_evidence", "logo_url", "brand_metadata"):
                    if data[field] and getattr(prior, field) and data[field] != getattr(prior, field):
                        conflict_fields.append(field)
            status = (CompanyProfileObservation.Status.CONFLICTED if conflict_fields
                      else CompanyProfileObservation.Status.AUTO_APPLIED if organization
                      else CompanyProfileObservation.Status.PENDING)
            with transaction.atomic():
                observation = CompanyProfileObservation.objects.create(
                    organization=organization,
                    observed_at=observed_at,
                    extraction_version=EXTRACTION_VERSION,
                    status=status,
                    conflict_fields=sorted(set(conflict_fields)),
                    fingerprint=fp,
                    **data,
                )
            counts["observations"] += 1
            counts[status] += 1
        except Exception as exc:
            counts["errors"] += 1
            reasons.append(_safe_reason(exc))
    age = max(0.0, (timezone.now() - observed_at).total_seconds())
    monitoring.record_event(
        "source_stage",
        {
            "stage": "company_profile_crawl",
            "status": "completed",
            "items_seen": len(items[:MAX_ITEMS]),
            "items_succeeded": counts["observations"],
            "items_failed": counts["errors"],
            "freshness_seconds": age,
        },
    )
    return CompanyCrawlResult(freshness_seconds=age, error_reasons=tuple(reasons), **counts)


__all__ = ["CompanyCrawlResult", "EXTRACTION_VERSION", "PROFILE_SCHEMA", "crawl_company_profile"]
