# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Safe Firecrawl adapter for code-approved company career sites.

The provider client is deliberately small and injectable.  It sends only a
bounded extraction request to Firecrawl and returns structured JSON; page
content is never retained by this adapter.  The career-site URL is validated
against the code-owned source allowlist before it is sent to the provider.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import time
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

import requests
from django.conf import settings

from crank.agents.jobs.base import (
    APPROVED_JOB_SOURCE_DOMAINS,
    JobSourceAdapter,
    JobSourceQuery,
    JobSourceResult,
    RawJobListing,
    validate_job_url,
)
from crank.agents.jobs.errors import (
    JobSourceDisabled,
    SourceBudgetExceededError,
)
from crank.agents.jobs.registry import register_job_adapter
from crank.agents.sources import errors as source_errors
from crank.services import monitoring

BlockedRedirectError = source_errors.BlockedRedirectError
SchemaDriftError = source_errors.SchemaDriftError
SourceServerError = source_errors.SourceServerError
SourceTimeoutError = source_errors.SourceTimeoutError
UnauthorizedSourceError = source_errors.UnauthorizedSourceError


EXTRACTION_VERSION = "firecrawl-careers.v1"
# This is sent to Firecrawl's structured extraction endpoint.  Keeping the
# schema in code prevents a catalog row from changing the provider contract.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["title", "canonical_url", "employer", "source_id"],
    "properties": {
        "title": {"type": "string"},
        "canonical_url": {"type": "string", "format": "uri"},
        "employer": {"type": "string"},
        "location": {"type": "string"},
        "remote_status": {"type": ["boolean", "string"]},
        "compensation": {"type": ["object", "string"]},
        "description_excerpt": {"type": "string"},
        "source_id": {"type": "string"},
        "status": {"type": "string", "enum": ["active", "closed", "expired"]},
    },
    "additionalProperties": False,
}


def _setting(name: str, env_name: str, default: Any) -> Any:
    try:
        value = getattr(settings, name)
    except Exception:  # pragma: no cover - settings are available in Django
        value = None
    if value is None or value == "":
        value = os.environ.get(env_name, default)
    return value


def _metric(name: str, value: float = 1) -> None:
    """Emit only low-cardinality Firecrawl operational counters."""
    monitoring.record_metric(name, value)


def _text(value: Any, field: str, *, required: bool = False, maximum: int = 2000) -> str:
    if not isinstance(value, str):
        raise SchemaDriftError(f"{field} must be a string")
    value = " ".join(value.strip().split())
    if required and not value:
        raise SchemaDriftError(f"{field} must be non-empty")
    if len(value) > maximum:
        raise SchemaDriftError(f"{field} exceeds {maximum} characters")
    return value


def _remote(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"remote", "fully remote", "true", "yes"}
    if value is None:
        return False
    raise SchemaDriftError("remote_status must be boolean or string")


def _compensation(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        # Free-form compensation is useful as an excerpt only when the
        # provider did not produce numeric fields.  RawJobListing has no such
        # field, so deliberately discard it rather than inventing numbers.
        return {}
    if not isinstance(value, Mapping):
        raise SchemaDriftError("compensation must be an object or string")
    result: dict[str, Any] = {}
    for destination, keys in {
        "compensation_min": ("min", "minimum", "compensation_min"),
        "compensation_max": ("max", "maximum", "compensation_max"),
        "compensation_currency": ("currency", "currency_code", "compensation_currency"),
        "compensation_interval": ("interval", "rate", "compensation_interval"),
    }.items():
        for key in keys:
            if key in value:
                result[destination] = value[key]
                break
    return result


class FirecrawlClient:
    """Minimal Firecrawl HTTP abstraction with bounded, credential-safe calls.

    ``request`` is injectable so tests never need a live provider.  The
    provider base URL is configuration, not a career-domain allowlist; the
    latter is enforced by :class:`FirecrawlCareersAdapter`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float,
        request: Callable[..., requests.Response] | None = None,
        max_bytes: int = 2 * 1024 * 1024,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise BlockedRedirectError("Firecrawl base URL must be HTTPS without credentials")
        if not api_key.strip():
            raise UnauthorizedSourceError("Firecrawl credentials are not configured")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._request = request or requests.request
        self._sleep = sleep

    def _endpoint(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _call(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        _metric("firecrawl_requests_total")
        try:
            response = self._request(
                method,
                self._endpoint(path),
                json=dict(payload or {}),
                headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            _metric("firecrawl_errors_total")
            raise SourceTimeoutError("Firecrawl request timed out") from exc
        except requests.RequestException as exc:
            _metric("firecrawl_errors_total")
            raise SourceServerError("Firecrawl request failed") from exc
        status = int(response.status_code)
        if status in {301, 302, 303, 307, 308}:
            _metric("firecrawl_errors_total")
            raise BlockedRedirectError("Firecrawl redirected unexpectedly")
        if status in {401, 403}:
            _metric("firecrawl_errors_total")
            raise UnauthorizedSourceError("Firecrawl credentials were rejected")
        if status >= 500 or status == 429:
            _metric("firecrawl_errors_total")
            raise SourceServerError("Firecrawl provider is unavailable")
        if status < 200 or status >= 300:
            _metric("firecrawl_errors_total")
            raise SchemaDriftError("Firecrawl returned an unexpected status")
        body = getattr(response, "content", b"")
        if not isinstance(body, bytes) or len(body) > self.max_bytes:
            _metric("firecrawl_errors_total")
            raise SchemaDriftError("Firecrawl response exceeded the configured size limit")
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _metric("firecrawl_errors_total")
            raise SchemaDriftError("Firecrawl response was not valid JSON") from exc
        if not isinstance(result, Mapping):
            _metric("firecrawl_errors_total")
            raise SchemaDriftError("Firecrawl response must be an object")
        return result

    def crawl_url(
        self,
        url: str,
        *,
        max_pages: int,
        max_listings: int,
        credit_budget: int,
        extraction_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if credit_budget < 1:
            raise SourceBudgetExceededError("Firecrawl credit budget is exhausted")
        limit = min(max_pages, max_listings, credit_budget)
        payload = {
            "url": url,
            "limit": limit,
            "scrapeOptions": {
                "formats": ["json"],
                "jsonOptions": {"schema": dict(extraction_schema)},
            },
        }
        started = self._call("POST", "/v1/crawl", payload)
        job_id = started.get("id")
        if "data" in started or str(started.get("status", "")).lower() in {"completed", "partial"}:
            _metric("firecrawl_credits_total", limit)
            return started
        if not isinstance(job_id, str) or not job_id or len(job_id) > 256:
            _metric("firecrawl_errors_total")
            raise SchemaDriftError("Firecrawl crawl response did not contain a job ID")
        # Firecrawl's crawl endpoint is asynchronous.  A bounded poll count
        # prevents an unresponsive provider from consuming the whole run.
        for _ in range(max_pages):
            result = self._call("GET", f"/v1/crawl/{job_id}")
            status = str(result.get("status", "")).lower()
            if status in {"completed", "partial", "failed", "cancelled"} or "data" in result:
                _metric("firecrawl_credits_total", limit)
                if status in {"failed", "cancelled"} and not result.get("data"):
                    raise SourceServerError("Firecrawl crawl did not complete")
                return result
            self._sleep(0)
        _metric("firecrawl_errors_total")
        raise SourceTimeoutError("Firecrawl crawl polling timed out")


@register_job_adapter
class FirecrawlCareersAdapter(JobSourceAdapter):
    """Normalize structured Firecrawl career results into ``RawJobListing``."""

    key = "firecrawl-careers"
    version = EXTRACTION_VERSION

    def __init__(self, source, *, client: FirecrawlClient | None = None) -> None:
        super().__init__(source)
        self._validate_source(source)
        if not bool(_setting("FIRECRAWL_ENABLED", "FIRECRAWL_ENABLED", False)):
            raise JobSourceDisabled("Firecrawl careers adapter is disabled")
        api_key = str(_setting("FIRECRAWL_API_KEY", "FIRECRAWL_API_KEY", "")).strip()
        if not api_key:
            raise UnauthorizedSourceError("Firecrawl credentials are not configured")
        self.max_pages = max(1, int(_setting("FIRECRAWL_MAX_PAGES", "FIRECRAWL_MAX_PAGES", 10)))
        self.max_listings = max(1, int(_setting("FIRECRAWL_MAX_LISTINGS", "FIRECRAWL_MAX_LISTINGS", 100)))
        self.credit_budget = max(0, int(_setting("FIRECRAWL_CREDIT_BUDGET", "FIRECRAWL_CREDIT_BUDGET", 10)))
        if client is not None:
            self.client = client
        else:
            self.client = FirecrawlClient(
                base_url=str(_setting("FIRECRAWL_BASE_URL", "FIRECRAWL_BASE_URL", "https://api.firecrawl.dev")),
                api_key=api_key,
                timeout=float(_setting("FIRECRAWL_TIMEOUT", "FIRECRAWL_TIMEOUT", 30.0)),
                max_bytes=int(_setting("JOBS_ADAPTER_MAX_BYTES", "JOBS_ADAPTER_MAX_BYTES", 2 * 1024 * 1024)),
            )

    def _validate_source(self, source) -> None:
        try:
            parsed = urlsplit(str(source.base_url))
            host = parsed.hostname.lower() if parsed.hostname else ""
            validate_job_url(str(source.base_url), allow_hosts=APPROVED_JOB_SOURCE_DOMAINS)
        except Exception as exc:
            raise BlockedRedirectError("career source URL is not approved") from exc
        if not host:  # pragma: no cover - validate_job_url already rejects empty hosts
            raise BlockedRedirectError("career source URL has no hostname")
        if host.rstrip(".") not in APPROVED_JOB_SOURCE_DOMAINS:  # pragma: no cover - validate_job_url already checks allowlist
            raise BlockedRedirectError("career source domain is not code-approved")
        self.source_host = host.rstrip(".")
        if getattr(source, "adapter_key", self.key).lower() != self.key:
            raise BlockedRedirectError("source is not cataloged for Firecrawl careers")

    def fetch(self, query: JobSourceQuery) -> JobSourceResult:
        max_pages = min(query.max_pages, self.max_pages)
        max_listings = min(query.max_listings, self.max_listings)
        if self.credit_budget < 1:
            raise SourceBudgetExceededError("Firecrawl credit budget is exhausted")
        try:
            response = self.client.crawl_url(
                str(self.source.base_url),
                max_pages=max_pages,
                max_listings=max_listings,
                credit_budget=self.credit_budget,
                extraction_schema=EXTRACTION_SCHEMA,
            )
            data = response.get("data", [])
            if not isinstance(data, list):
                raise SchemaDriftError("Firecrawl data must be a list")
            listings = []
            for item in data[:max_listings]:
                listings.append(self._listing(item, response.get("id", "")))
            return JobSourceResult(
                listings=tuple(listings),
                pages_fetched=min(max_pages, len(data)),
                items_seen=len(data),
            )
        except (SourceBudgetExceededError, SourceTimeoutError, UnauthorizedSourceError, BlockedRedirectError, SchemaDriftError, SourceServerError):
            raise
        except Exception as exc:
            _metric("firecrawl_errors_total")
            raise SchemaDriftError("Firecrawl extraction failed") from exc

    def _listing(self, item: Any, crawl_job_id: Any) -> RawJobListing:
        if not isinstance(item, Mapping):
            raise SchemaDriftError("Firecrawl listing must be an object")
        extracted = item.get("extract", item.get("json", item))
        if not isinstance(extracted, Mapping):
            raise SchemaDriftError("Firecrawl extraction must be an object")
        canonical_url = _text(extracted.get("canonical_url"), "canonical_url", required=True, maximum=750)
        parsed = urlsplit(canonical_url)
        if (parsed.hostname or "").lower().rstrip(".") != self.source_host:
            raise BlockedRedirectError("Firecrawl listing URL is outside the approved career domain")
        canonical_url = validate_job_url(canonical_url, allow_hosts={self.source_host})
        now = datetime.now(timezone.utc)
        metadata = item.get("metadata", {})
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, Mapping):
            raise SchemaDriftError("Firecrawl metadata must be an object")
        source_url = metadata.get("sourceURL", self.source.base_url)
        source_url = validate_job_url(_text(source_url, "source_url", required=True, maximum=750), allow_hosts={self.source_host})
        compensation = _compensation(extracted.get("compensation"))
        for field in ("compensation_min", "compensation_max", "compensation_currency", "compensation_interval"):
            if field in extracted and field not in compensation:
                compensation[field] = extracted[field]
        status = _text(extracted.get("status", "active"), "status", maximum=8).lower()
        if status not in {"active", "closed", "expired"}:
            raise SchemaDriftError("status is unsupported")
        source_id = _text(extracted.get("source_id"), "source_id", required=True, maximum=256)
        return RawJobListing(
            external_id=source_id,
            canonical_url=canonical_url,
            employer_name=_text(extracted.get("employer"), "employer", required=True, maximum=200),
            title=_text(extracted.get("title"), "title", required=True, maximum=300),
            location_text=_text(extracted.get("location", ""), "location", maximum=500),
            is_remote=_remote(extracted.get("remote_status", False)),
            description_excerpt=_text(extracted.get("description_excerpt", ""), "description_excerpt", maximum=2000),
            first_seen_at=now,
            last_seen_at=now,
            status=status,
            source_metadata={
                "source_url": source_url,
                "crawl_job_id": _text(crawl_job_id, "crawl_job_id", maximum=256),
                "extraction_version": EXTRACTION_VERSION,
                "observed_at": now.isoformat(),
            },
            **compensation,
        )


__all__ = [
    "EXTRACTION_SCHEMA",
    "EXTRACTION_VERSION",
    "FirecrawlCareersAdapter",
    "FirecrawlClient",
]
