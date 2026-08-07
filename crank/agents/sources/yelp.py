# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Yelp Fusion API source adapter (approved MVP vertical slice).

The Yelp Fusion ``/v3/businesses/search`` endpoint is a lawful, official API
(documentation: https://docs.developer.yelp.com). It authenticates with a
bearer API key, paginates via ``offset``/``limit``, and returns JSON. This
adapter parses its ``businesses`` list into typed :class:`RawScoreObservation`
records carrying the adapter/version metadata.

Security
--------
* All requests go through :class:`SafeHTTPClient` with an exact allowlist of
  ``api.yelp.com``, HTTPS enforcement, bounded redirects, a response-size cap,
  a ``application/json`` content-type check, private/link-local/loopback
  rejection after DNS resolution and redirects, and bounded transient retries.
* The API key is read from the environment (``YELP_API_KEY``), never logged,
  and never included in errors. The adapter fails closed when it is missing.
* No live network calls occur in the test suite; tests drive the injected
  ``http`` client with recorded, sanitized fixtures.

Auth
----
Requires ``YELP_API_KEY`` set in the environment. See ``crank/settings/base.py``
for the ``YELP_*`` configuration knobs.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any, Dict, List, Mapping, Optional

from crank.agents.sources import errors
from crank.agents.sources.contract import (
    RawScoreObservation,
    SourceQuery,
    SourceResult,
)
from crank.agents.sources.transport import SafeHTTPClient

logger = logging.getLogger(__name__)

#: Stable adapter key for registration/factory use.
ADAPTER_KEY = "yelp"

#: Adapter implementation version (recorded as provenance metadata).
ADAPTER_VERSION = "1.0.0"

#: Exact allowlisted hosts for this source.
ALLOWED_HOSTS = ("api.yelp.com",)

#: API endpoint path (appended to a base URL that already includes ``/v3``).
SEARCH_PATH = "/businesses/search"

#: Expected content type.
CONTENT_TYPE = "application/json"

#: Yelp's max ``limit`` per request.
MAX_LIMIT = 50

#: Default max pages fetched in one run (bounded pagination).
DEFAULT_MAX_PAGES = 20

#: Default ceiling on observations per run.
DEFAULT_MAX_OBSERVATIONS = 500

#: Default base URL (configurable for future staging mirrors; keep HTTPS).
DEFAULT_BASE_URL = "https://api.yelp.com/v3"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class YelpSourceAdapter:
    """Fetches company ratings from the Yelp Fusion API.

    Implements the :class:`SourceAdapter` protocol from ``contract.py``.
    """

    key = ADAPTER_KEY
    version = ADAPTER_VERSION
    allowed_hosts = ALLOWED_HOSTS
    expected_content_type = CONTENT_TYPE

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        http: Optional[SafeHTTPClient] = None,
        base_url: Optional[str] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_observations: int = DEFAULT_MAX_OBSERVATIONS,
        run_correlation_id: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or _env("YELP_API_KEY")
        if not self.api_key:
            raise errors.UnauthorizedSourceError(
                "YELP_API_KEY is not configured; YelpSourceAdapter refuses to start"
            )
        # The raw key is sent as a Bearer token; it is attached per-request and
        # never logged nor included in errors by SafeHTTPClient.
        self.base_url = (base_url or _env("YELP_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.max_pages = max_pages
        self.max_observations = max_observations
        self.run_correlation_id = run_correlation_id or _env("AGENT_RUN_CORRELATION_ID") or None

        self._http = http or SafeHTTPClient(
            allowed_hosts=self.allowed_hosts,
            expected_content_type=self.expected_content_type,
            max_bytes=int(_env("YELP_MAX_BYTES") or 2 * 1024 * 1024),
            max_redirects=int(_env("YELP_MAX_REDIRECTS") or 5),
            timeout=(float(_env("YELP_CONNECT_TIMEOUT") or 3.05), float(_env("YELP_READ_TIMEOUT") or 30)),
            max_transient_attempts=int(_env("YELP_MAX_TRANSIENT_ATTEMPTS") or 4),
            auth_headers={"Authorization": f"Bearer {self.api_key}"},
        )
        self._search_url = f"{self.base_url}{SEARCH_PATH}"

    # ------------------------------------------------------------------
    # adapter protocol
    # ------------------------------------------------------------------

    def fetch(self, query: SourceQuery) -> SourceResult:
        """Fetch up to ``max_observations`` raw observations for ``query``.

        Paginates through the search endpoint at most ``max_pages`` times,
        respecting Yelp's ``offset``/``limit`` model. Errors are typed
        :class:`SourceError` subclasses surfaced by :class:`SafeHTTPClient`.
        """
        observations: List[RawScoreObservation] = []
        offset = 0
        pages = 0
        while pages < self.max_pages and len(observations) < self.max_observations:
            status, headers, body = self._http.get(
                self._search_url,
                params=self._params(query, offset=offset, limit=MAX_LIMIT),
            )
            payload = self._parse_payload(body)
            businesses = self._parse_businesses(payload, url=self._search_url)
            fetched_at = datetime.datetime.now(datetime.timezone.utc)
            for entry in businesses:
                observations.append(self._to_observation(entry, fetched_at=fetched_at))
            pages += 1

            total = self._total(payload)
            offset += MAX_LIMIT
            if not businesses or (total is not None and offset >= total):
                break
            # Prevent UI-hang/fan-out: never fetch more than the configured
            # budget no matter what the source reports.
            if offset >= self.max_observations:
                break

        return SourceResult(
            observations=observations,
            pages_fetched=pages,
            items_seen=len(observations),
        )

    # ------------------------------------------------------------------
    # parse helpers
    # ------------------------------------------------------------------

    def _params(self, query: SourceQuery, *, offset: int, limit: int) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if query.term:
            params["term"] = query.term
        if query.location:
            params["location"] = query.location
        return params

    def _parse_payload(self, body: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise errors.MalformedPayloadError("response was not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise errors.SchemaDriftError("top-level payload is not a JSON object")
        return payload

    def _parse_businesses(self, payload: Mapping[str, Any], *, url: str) -> List[Mapping[str, Any]]:
        businesses = payload.get("businesses", [])
        if not isinstance(businesses, list):
            raise errors.SchemaDriftError("payload 'businesses' is not a list")
        if any(not isinstance(b, dict) for b in businesses):
            raise errors.SchemaDriftError("payload contains a non-object business")
        return [b for b in businesses if b]

    def _total(self, payload: Mapping[str, Any]) -> Optional[int]:
        total = payload.get("total")
        if total is None:
            return None
        if not isinstance(total, int):
            raise errors.SchemaDriftError("payload 'total' is not an integer")
        if total < 0:
            raise errors.SchemaDriftError("payload 'total' is negative")
        return total

    def _to_observation(
        self, business: Mapping[str, Any], *, fetched_at: datetime.datetime
    ) -> RawScoreObservation:
        business_id = business.get("id")
        name = business.get("name")
        rating = business.get("rating")
        url = business.get("url")

        if not isinstance(business_id, str) or not business_id:
            raise errors.SchemaDriftError("business missing a string 'id'")
        if not isinstance(name, str) or not name:
            raise errors.SchemaDriftError("business missing a string 'name'")
        if not isinstance(rating, (int, float)) or isinstance(rating, bool):
            raise errors.SchemaDriftError("business 'rating' is not a number")
        if not isinstance(url, str) or not url:
            # Rating without a canonical URL is unusable for provenance.
            raise errors.SchemaDriftError("business missing a string 'url'")

        rating = float(rating)
        if not (0 <= rating <= 5):
            raise errors.SchemaDriftError(f"business 'rating' {rating!r} out of range")

        observed_at = self._observed_at(business)
        return RawScoreObservation.create(
            external_id=business_id,
            source_url=url,
            target_identity=name,
            score_type="rating",
            value=rating,
            range_low=0.0,
            range_high=5.0,
            observed_at=observed_at,
            fetched_at=fetched_at,
            adapter=self.key,
            adapter_version=self.version,
            run_correlation_id=self.run_correlation_id,
        )

    def _observed_at(self, business: Mapping[str, Any]) -> datetime.datetime:
        # Yelp does not expose a stable per-business rating timestamp in the
        # search response; use the fetch time as the observed time. Kept as a
        # separate seam so a future richer endpoint can supply a real one.
        return datetime.datetime.now(datetime.timezone.utc)


__all__ = ["ADAPTER_KEY", "ADAPTER_VERSION", "YelpSourceAdapter"]
