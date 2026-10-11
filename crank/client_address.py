# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The only place a client network address is derived (issue #538).

Every request reaches the app from a node address, so ``REMOTE_ADDR`` never
identifies a client, and ``X-Forwarded-For`` is client-controlled. An address
is resolved only in production, only when ``CLIENT_IP_SOURCE`` names a known
source, and only from that source's single header. Anyone who can open a
connection to the pod can send that header, so the setting must stay empty
until the operator gate in ``docs/runbook-client-address.md`` has passed.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import NamedTuple

from django.conf import settings
from django.core.cache import cache
from django.utils.crypto import salted_hmac

logger = logging.getLogger(__name__)

#: The whole list of sources. The setting names a source, never a header, so
#: an operator cannot point it at a client-controlled header by typing a value.
TRUSTED_HEADERS = {"cloudflare": "HTTP_CF_CONNECTING_IP"}

#: Longest textual IP literal (an IPv4-mapped IPv6 address).
MAX_ADDRESS_LENGTH = 45
#: One subscriber normally holds at least a /64 and can use any address in it.
IPV6_BUCKET_PREFIX = 64

WARNING_CACHE_KEY = "client-address-unavailable-warning"
WARNING_INTERVAL_SECONDS = 10 * 60

_KEY_SALT = "crank.client_address"
_KEY_LENGTH = 32


class ClientAddress(NamedTuple):
    """An identified client: ``bucket`` is clear text, ``key`` a keyed digest."""

    bucket: str
    key: str


def effective_source() -> str:
    """The trusted source in force, or ``""`` when no address may be read."""
    if str(getattr(settings, "ENV", "")).strip().lower() != "prod":
        return ""
    value = getattr(settings, "CLIENT_IP_SOURCE", "")
    return value if isinstance(value, str) and value in TRUSTED_HEADERS else ""


def _bucket(value):
    """Return ``(bucket, None)`` for exactly one IP literal, else ``(None, reason)``."""
    if not isinstance(value, str) or not value.strip():
        return None, "missing"
    if "," in value:
        return None, "multiple"
    value = value.strip()
    if len(value) > MAX_ADDRESS_LENGTH or "%" in value:
        return None, "invalid"
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None, "invalid"
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.version == 4:
        return str(address), None
    network = ipaddress.IPv6Network((int(address), IPV6_BUCKET_PREFIX), strict=False)
    return str(network), None


def _warn_unavailable(source, reason):
    """Log at most once per interval across the deployment; never the value."""
    if cache.add(WARNING_CACHE_KEY, 1, WARNING_INTERVAL_SECONDS):
        logger.warning(
            "client address unavailable: source=%s reason=%s", source, reason
        )


def client_address(request) -> ClientAddress | None:
    """The request's client address, or None when it is off or unidentified."""
    source = effective_source()
    if not source:
        return None
    bucket, reason = _bucket(request.META.get(TRUSTED_HEADERS[source]))
    if bucket is None:
        _warn_unavailable(source, reason)
        return None
    key = salted_hmac(_KEY_SALT, bucket).hexdigest()[:_KEY_LENGTH]
    return ClientAddress(bucket, key)


__all__ = ["ClientAddress", "TRUSTED_HEADERS", "client_address", "effective_source"]
