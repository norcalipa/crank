# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Code-owned allowlist of approved external rating-source base domains.

Issue #310 owns the decision of which external authority the first source
adapter may lawfully pull from. Until that approval is recorded, no real
external host is permitted: a source must be added here (in code, reviewed in
a PR) before a ``SourceCatalog.base_url`` on that domain can be used. This is a
deliberate SSRF guard -- base domains come from code, never from database rows
or issue comments.

Domain matching is suffix-based and normalized to lowercase, so a subdomain of
an approved domain (e.g. ``api.ratings.example.test``) is allowed while an
unrelated host that merely *resembles* one is not. All matches are validated
strictly by :func:`crank.agents.sources.base.validate_source_base_url`.
"""

from __future__ import annotations

#: Approved external source base domains (host:port-less). Add the authority
#: approved by issue #310 here in a code review; do not add domains without an
#: approval record. Example/test domains below let adapters and tests exercise
#: the allowlist without implying a real vendor exposes a public API.
APPROVED_SOURCE_DOMAINS: frozenset[str] = frozenset(
    {
        "ratings.example.test",
        "api.example.test",
    }
)


def _normalize_host(host: str) -> str:
    """Lowercase and strip a trailing dot from a hostname."""
    return (host or "").strip().strip(".").lower()


def is_domain_allowed(host: str) -> bool:
    """Return True if ``host`` (a bare hostname, no scheme/port) is on the
    code-owned allowlist or is a subdomain of an allowlisted domain."""
    host = _normalize_host(host)
    if not host:
        return False
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in APPROVED_SOURCE_DOMAINS
    )
