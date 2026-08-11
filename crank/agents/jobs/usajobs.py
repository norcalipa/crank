# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""USAJOBS Search adapter.

Only the official JSON Search endpoint is used.  Response text is treated as
untrusted display data: it is normalized by ``RawJobListing`` and is never
copied into provenance metadata or logs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from typing import Any, Mapping
from urllib.parse import urlsplit

from django.conf import settings

from crank.agents.jobs.base import (
    APPROVED_JOB_SOURCE_DOMAINS,
    JobSourceAdapter,
    JobSourceQuery,
    JobSourceResult,
    RawJobListing,
)
from crank.agents.jobs.registry import register_job_adapter
from crank.agents.sources import errors as source_errors
from crank.agents.sources.transport import SafeHTTPClient


API_HOST = "data.usajobs.gov"
API_PATH = "/api/Search"
MAX_PAGE_SIZE = 500


def _setting_or_env(setting_name: str, env_name: str, default: Any = "") -> Any:
    try:
        value = getattr(settings, setting_name)
    except Exception:
        value = None
    if not value:
        value = os.environ.get(env_name, default)
    return value


def _text(value: Any, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise source_errors.SchemaDriftError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise source_errors.SchemaDriftError(f"{field} must be non-empty")
    return value


def _optional_text(value: Any, field: str) -> str:
    if value is None:
        return ""
    return _text(value, field)


def _number(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise source_errors.SchemaDriftError(f"{field} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise source_errors.SchemaDriftError(f"{field} must be numeric") from exc
    if not number.is_finite() or number < 0:
        raise source_errors.SchemaDriftError(f"{field} must be finite and non-negative")
    return number


def _date(value: Any, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    value = _text(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise source_errors.SchemaDriftError(f"{field} is not an ISO date") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _nested(mapping: Mapping[str, Any], *path: str) -> Any:
    value: Any = mapping
    for key in path:
        if not isinstance(value, Mapping):
            raise source_errors.SchemaDriftError(f"{'.'.join(path)} must be an object")
        value = value.get(key)
    return value


@register_job_adapter
class USAJobsAdapter(JobSourceAdapter):
    """Fetch and normalize bounded pages from USAJOBS Search."""

    key = "usajobs"
    version = "1.0.0"

    def __init__(
        self,
        source,
        *,
        http: SafeHTTPClient | None = None,
        auth_key: str | None = None,
        user_agent_email: str | None = None,
        resolver=None,
        use_requests=None,
    ) -> None:
        super().__init__(source)
        self.auth_key = (auth_key if auth_key is not None else _setting_or_env("USAJOBS_AUTH_KEY", "USAJOBS_AUTH_KEY")).strip()
        self.user_agent_email = (user_agent_email if user_agent_email is not None else _setting_or_env("USAJOBS_USER_AGENT_EMAIL", "USAJOBS_USER_AGENT_EMAIL")).strip()
        if not self.auth_key or not self.user_agent_email:
            raise source_errors.UnauthorizedSourceError(
                "USAJOBS credentials are not configured; adapter refuses to start"
            )
        base_url = str(source.base_url).rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.hostname != API_HOST:
            raise source_errors.BlockedRedirectError("USAJOBS API host is not approved")
        self.search_url = base_url if parsed.path.rstrip("/").endswith(API_PATH) else base_url + API_PATH
        if http is not None:
            self._http = http
        else:
            timeout = float(_setting_or_env("JOBS_ADAPTER_TIMEOUT", "JOBS_ADAPTER_TIMEOUT", 30.0))
            max_bytes = int(_setting_or_env("JOBS_ADAPTER_MAX_BYTES", "JOBS_ADAPTER_MAX_BYTES", 2 * 1024 * 1024))
            self._http = SafeHTTPClient(
                allowed_hosts=[host for host in APPROVED_JOB_SOURCE_DOMAINS if host == API_HOST],
                expected_content_type="application/json",
                max_bytes=max_bytes,
                timeout=(timeout, timeout),
                resolver=resolver,
                use_requests=use_requests,
                auth_headers={
                    "Authorization-Key": self.auth_key,
                    "User-Agent": self.user_agent_email,
                },
            )

    def fetch(self, query: JobSourceQuery) -> JobSourceResult:
        listings: list[RawJobListing] = []
        offset = 0
        pages = 0
        items_seen = 0
        while pages < query.max_pages and len(listings) < query.max_listings:
            limit = min(MAX_PAGE_SIZE, query.max_listings - len(listings))
            params: dict[str, Any] = {"offset": offset, "limit": limit}
            if query.keyword:
                params["Keyword"] = query.keyword
            if query.location:
                params["LocationName"] = query.location
            _, _, body = self._http.get(self.search_url, params=params)
            payload = self._payload(body)
            entries, total = self._entries(payload)
            pages += 1
            items_seen += len(entries)
            for entry in entries:
                if len(listings) >= query.max_listings:
                    break
                listings.append(self._listing(entry))
            offset += limit
            if not entries or (total is not None and offset >= total) or (total is None and len(entries) < limit):
                break
        return JobSourceResult(listings=tuple(listings), pages_fetched=pages, items_seen=items_seen)

    @staticmethod
    def _payload(body: bytes) -> Mapping[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise source_errors.MalformedPayloadError("response was not valid UTF-8 JSON") from exc
        if not isinstance(payload, Mapping):
            raise source_errors.SchemaDriftError("top-level payload must be an object")
        return payload

    @staticmethod
    def _entries(payload: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], int | None]:
        result = payload.get("SearchResult")
        if not isinstance(result, Mapping):
            raise source_errors.SchemaDriftError("payload is missing SearchResult object")
        raw_entries = result.get("SearchResultItems")
        if not isinstance(raw_entries, list):
            raise source_errors.SchemaDriftError("SearchResultItems must be a list")
        if any(not isinstance(item, Mapping) for item in raw_entries):
            raise source_errors.SchemaDriftError("SearchResultItems must contain objects")
        total = result.get("SearchResultCount")
        if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
            raise source_errors.SchemaDriftError("SearchResultCount must be a non-negative integer")
        return list(raw_entries), total

    def _listing(self, item: Mapping[str, Any]) -> RawJobListing:
        descriptor = item.get("MatchedObjectDescriptor")
        if not isinstance(descriptor, Mapping):
            raise source_errors.SchemaDriftError("listing is missing MatchedObjectDescriptor")
        external_id = _text(item.get("MatchedObjectId"), "MatchedObjectId", required=True)
        canonical_url = _text(descriptor.get("PositionURI"), "PositionURI", required=True)
        employer = _text(descriptor.get("OrganizationName"), "OrganizationName", required=True)
        title = _text(descriptor.get("PositionTitle"), "PositionTitle", required=True)
        location = _optional_text(descriptor.get("PositionLocationDisplay"), "PositionLocationDisplay")
        remote = descriptor.get("RemoteIndicator", False)
        if not isinstance(remote, bool):
            raise source_errors.SchemaDriftError("RemoteIndicator must be boolean")
        remuneration = descriptor.get("PositionRemuneration", [])
        if not isinstance(remuneration, list) or any(not isinstance(value, Mapping) for value in remuneration):
            raise source_errors.SchemaDriftError("PositionRemuneration must be a list of objects")
        pay = remuneration[0] if remuneration else {}
        minimum = _number(pay.get("MinimumRange"), "MinimumRange")
        maximum = _number(pay.get("MaximumRange"), "MaximumRange")
        currency = _optional_text(pay.get("CurrencyCode", pay.get("Currency")), "CurrencyCode")
        interval = _optional_text(pay.get("RateIntervalCode"), "RateIntervalCode")
        description = descriptor.get("PositionFormattedDescription", "")
        if not description:
            details = _nested(descriptor, "UserArea", "Details") if "UserArea" in descriptor else {}
            if details is not None and not isinstance(details, Mapping):
                raise source_errors.SchemaDriftError("UserArea.Details must be an object")
            description = (details or {}).get("JobSummary", "")
        description = _optional_text(description, "description")
        end_date = _date(descriptor.get("PositionEndDate"), "PositionEndDate")
        status_value = descriptor.get("PositionStatus", descriptor.get("JobStatus", ""))
        if status_value and not isinstance(status_value, str):
            raise source_errors.SchemaDriftError("PositionStatus must be a string")
        status = "closed" if str(status_value).strip().lower() == "closed" else "active"
        now = datetime.now(timezone.utc)
        if end_date is not None and end_date < now:
            status = "expired"
        metadata = {"adapter": self.key, "adapter_version": self.version}
        return RawJobListing(
            external_id=external_id,
            canonical_url=canonical_url,
            employer_name=employer,
            title=title,
            location_text=location,
            is_remote=remote,
            compensation_min=minimum,
            compensation_max=maximum,
            compensation_currency=currency,
            compensation_interval=interval,
            description_excerpt=description,
            first_seen_at=now,
            last_seen_at=now,
            status=status,
            source_metadata=metadata,
        )


USAJobsSourceAdapter = USAJobsAdapter
USAJOBSAdapter = USAJobsAdapter

__all__ = ["USAJobsAdapter", "USAJobsSourceAdapter", "USAJOBSAdapter"]
