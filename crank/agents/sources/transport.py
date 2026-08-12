# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Hardened HTTPS transport for source adapters.

``SafeHTTPClient`` is the only network path available to source adapters. It
enforces:

* HTTPS only and an **exact** hostname allowlist (no suffix/prefix matching).
* Manual redirect following with a bounded redirect count; every redirect hop is
  re-validated against the allowlist and re-resolved for address safety.
* Rejection of private, link-local, loopback, and otherwise unroutable
  addresses **after DNS resolution** and **after every redirect hop**.
* A maximum response body size enforced **before** parsing.
* An expected Content-Type check.
* Connect/read timeouts.
* Bounded transient retries with exponential backoff and jitter (throttle and
  5xx), including ``Retry-After`` for throttling responses.

Auth headers are never included in raised errors or logs. The transport accepts
injected ``request``/``resolver`` callables so tests can drive every security
path without touching the network.
"""

from __future__ import annotations

import ipaddress
import logging
import random
import re
import socket
import time
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import requests

from crank.agents.sources import errors

logger = logging.getLogger(__name__)

#: Types of response we treat as redirects and follow (bounded).
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
#: Default ceiling on how many redirects we will follow.
_DEFAULT_MAX_REDIRECTS = 5
#: Default max response body bytes.
_DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
#: Default connect/read timeouts (seconds), mirroring requests tuple form.
_DEFAULT_TIMEOUT = (3.05, 30)
#: Default bounded transient attempts (initial request + retries).
_DEFAULT_MAX_TRANSIENT_ATTEMPTS = 4
#: Default base backoff seconds before growing exponentially.
_DEFAULT_BACKOFF_BASE = 0.5
#: Default maximum backoff ceiling (seconds).
_DEFAULT_BACKOFF_MAX = 8.0

#: Normalized schemes accepted for source URLs (https only).
_ALLOWED_SCHEMES = {"https"}

#: Exact network addresses that are always disallowed regardless of allowlist.
_PRIVATE_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:0:0/96"),  # IPv4-mapped
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64
    ipaddress.ip_network("100::/64"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("2001:10::/28"),  # ORCHID
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


def address_is_blocked(address: str) -> bool:
    """Return True when ``address`` is private/link-local/loopback/unspecified.

    Accepts IPv4 or IPv6 address strings (including if present) and rejects
    anything that resolves into the reserved private/link-local/loopback
    ranges. This runs after ``getaddrinfo`` and after every redirect hop.
    """
    ip_str = address.split("%")[0]
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # Not a parseable literal; treat conservatively as blocked.
        return True
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return True
    for net in _PRIVATE_NETS:
        if ip in net:
            return True
    return False


def _default_resolver(host: str) -> Iterable[str]:
    """Resolve ``host`` to address strings via ``getaddrinfo``."""
    infos = socket.getaddrinfo(host, None)
    seen: set = set()
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.add(addr)
            yield addr


RequestCallable = Callable[..., requests.Response]
ResolverCallable = Callable[[str], Iterable[str]]


class SafeHTTPClient:
    """A request-tenant, single-purpose HTTPS client for source adapters.

    ``use_requests`` is the requests ``request`` callable (injected in tests)
    and ``resolver`` is a host->address-literal resolver (``socket.getaddrinfo``
    by default). Timeouts, byte caps, redirect ceilings, and retry bounds are
    configurable per client so a future source catalog can tune them.
    """

    def __init__(
        self,
        *,
        allowed_hosts: Sequence[str],
        expected_content_type: str,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_redirects: int = _DEFAULT_MAX_REDIRECTS,
        timeout: Tuple[float, float] = _DEFAULT_TIMEOUT,
        max_transient_attempts: int = _DEFAULT_MAX_TRANSIENT_ATTEMPTS,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        backoff_max: float = _DEFAULT_BACKOFF_MAX,
        use_requests: Optional[RequestCallable] = None,
        resolver: Optional[ResolverCallable] = None,
        auth_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("allowed_hosts must be non-empty")
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.expected_content_type = expected_content_type.lower()
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.timeout = timeout
        self.max_transient_attempts = max(1, max_transient_attempts)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._requests = use_requests or requests.request
        self._resolver = resolver or _default_resolver
        # Auth headers are attached per-request and never copied into errors.
        self._auth_headers = dict(auth_headers or {})

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Tuple[int, Mapping[str, str], bytes]:
        """GET ``url`` and return ``(status, headers, body)``.

        Applies the full security policy (allowlist, redirect ceiling, max
        bytes, content type, address checks) and bounded transient retries.
        Raises a typed :class:`SourceError` subclass on any failure.
        """
        request_headers = dict(self._auth_headers)
        if headers:
            request_headers.update(headers)

        self._assert_url_allowed(url)

        attempts = 0
        while True:
            attempts += 1
            try:
                return self._request_once(url, params=params, headers=request_headers)
            except (errors.SourceThrottledError, errors.SourceServerError, errors.SourceTimeoutError) as exc:
                if attempts >= self.max_transient_attempts:
                    raise errors.RetriesExhaustedError(
                        f"transient retries exhausted for {url!r} after "
                        f"{attempts} attempts (last: {exc.__class__.__name__})",
                        last_error=exc,
                    ) from exc
                delay = self._retry_delay(exc.retry_after, attempts)
                logger.warning(
                    "source transient failure (attempt %s/%s): %s; retrying in %.2fs",
                    attempts,
                    self.max_transient_attempts,
                    exc.__class__.__name__,
                    delay,
                )
                time.sleep(delay)

    # ------------------------------------------------------------------
    # request execution
    # ------------------------------------------------------------------

    def _request_once(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]],
        headers: Mapping[str, str],
    ) -> Tuple[int, Mapping[str, str], bytes]:
        current = url
        seen: set = set()
        for _ in range(self.max_redirects + 1):
            current = self._canonical(current)
            if current in seen:
                raise errors.BlockedRedirectError(f"redirect loop at {current!r}")
            seen.add(current)
            self._assert_url_allowed(current)
            self._assert_resolved_addresses(current)

            # Authorization is attached for the request but never logged nor
            # included in error text, so auth never leaks through exceptions.
            req_headers = dict(headers)
            req_headers["Accept"] = "application/json"

            try:
                response = self._requests(
                    "GET",
                    current,
                    params=params,
                    headers=req_headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.Timeout as exc:
                raise errors.SourceTimeoutError(f"timeout fetching {current!r}") from exc
            except requests.ConnectionError as exc:
                raise errors.SourceServerError(f"connection error fetching {current!r}") from exc
            except requests.RequestException as exc:
                raise errors.SourceServerError(f"request failed fetching {current!r}") from exc

            status = int(response.status_code)
            if status in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    raise errors.MalformedPayloadError(
                        f"redirect response {status} without Location for {current!r}"
                    )
                current = _absolute(current, location)
                # Re-validated at top of loop (allowlist + address re-check).
                continue

            # Classify status first so an auth/rate-limit/server error is never
            # misreported as a content-type/size/schema problem: error bodies are
            # not required to be JSON or fall under the payload byte budget.
            if status == 401 or status == 403:
                raise errors.UnauthorizedSourceError(
                    f"unauthorized ({status}) fetching {current!r}"
                )
            if status == 429:
                raise errors.SourceThrottledError(
                    f"throttled (429) fetching {current!r}",
                    retry_after=_retry_after_seconds(response.headers),
                )
            if status >= 500:
                raise errors.SourceServerError(f"server error ({status}) fetching {current!r}")
            if status != 200:
                raise errors.SourceServerError(f"unexpected status {status} fetching {current!r}")

            self._check_content_type(current, dict(response.headers))
            body = self._read_bounded(current, response)
            return status, dict(response.headers), body

        raise errors.BlockedRedirectError(f"too many redirects fetching {url!r}")

    # ------------------------------------------------------------------
    # policy helpers
    # ------------------------------------------------------------------

    def _canonical(self, url: str) -> str:
        return url.rstrip("#").rstrip("?")

    def _assert_url_allowed(self, url: str) -> None:
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            raise errors.SourceServerError(f"invalid URL {url!r}") from exc
        scheme = (parsed.scheme or "").lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise errors.BlockedRedirectError(
                f"non-HTTPS or missing scheme {scheme!r} for {url!r}"
            )
        if parsed.username is not None or parsed.password is not None:
            raise errors.BlockedRedirectError(
                f"URL credentials are not allowed for {url!r}"
            )
        if parsed.port not in (None, 443):
            raise errors.BlockedRedirectError(
                f"non-standard HTTPS port is not allowed for {url!r}"
            )
        host = (parsed.hostname or "").lower()
        if host not in self.allowed_hosts:
            raise errors.BlockedRedirectError(
                f"host {host!r} not in allowlist for {url!r}"
            )

    def _assert_resolved_addresses(self, url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        resolved = list(self._resolver(host))
        if not resolved:
            raise errors.BlockedAddressError(
                f"host {host!r} did not resolve to a globally routable address"
            )
        blocked: List[str] = []
        for addr in resolved:
            if address_is_blocked(addr):
                blocked.append(addr)
        if blocked:
            raise errors.BlockedAddressError(
                f"resolved address for {host!r} is not globally routable: "
                f"{', '.join(blocked)}"
            )

    def _check_content_type(self, url: str, response_headers: Mapping[str, str]) -> None:
        content_type = (response_headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != self.expected_content_type:
            raise errors.WrongContentTypeError(
                f"unexpected Content-Type {content_type!r} for {url!r} "
                f"(expected {self.expected_content_type!r})"
            )

    def _read_bounded(self, url: str, response: requests.Response) -> bytes:
        chunks: List[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > self.max_bytes:
                raise errors.OversizedPayloadError(
                    f"response for {url!r} exceeded {self.max_bytes} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _retry_delay(self, retry_after: Optional[float], attempt: int) -> float:
        if retry_after is not None and retry_after > 0:
            return float(retry_after)
        jitter = random.uniform(0.8, 1.2)
        delay = self.backoff_base * (2 ** (attempt - 1)) * jitter
        return min(delay, self.backoff_max)


def _absolute(base_url: str, location: str) -> str:
    return urljoin(base_url, location)


def _retry_after_seconds(header_map: Mapping[str, str]) -> Optional[float]:
    value = header_map.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def redact_auth(value: Any) -> str:
    """Best-effort redaction of an ``Authorization`` header value.

    Used when an arbitrary string (e.g. a repr) might carry a header. The real
    transport guarantee is stronger: auth headers are never included in raised
    errors at all. This is only a defensive net for values built elsewhere.
    """
    return re.sub(
        r"(?im)^([ \t]*authorization:[ \t]+).*$",
        r"\1***",
        str(value),
    )


__all__ = [
    "SafeHTTPClient",
    "address_is_blocked",
    "redact_auth",
]
