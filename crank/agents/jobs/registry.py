# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Code-owned job adapter registry and policy-gated factory."""

from __future__ import annotations

from crank.agents.jobs.base import JobSourceAdapter
from crank.agents.jobs.errors import (
    JobSourceBlocked,
    JobSourceDisabled,
    JobSourceNotApproved,
    UnknownJobAdapter,
    UnapprovedJobSource,
)
from crank.agents.jobs.base import validate_job_url


class JobAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, type[JobSourceAdapter]] = {}

    def register(self, adapter_cls: type[JobSourceAdapter]) -> type[JobSourceAdapter]:
        key = getattr(adapter_cls, "key", "")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("job adapter must define a non-empty key")
        normalized = key.lower()
        if normalized in self._adapters:
            raise ValueError(f"job adapter key {key!r} is already registered")
        self._adapters[normalized] = adapter_cls
        return adapter_cls

    def get(self, key: str):
        return self._adapters.get((key or "").lower())

    def keys(self) -> list[str]:
        return sorted(self._adapters)


REGISTRY = JobAdapterRegistry()


def register_job_adapter(adapter_cls: type[JobSourceAdapter]):
    return REGISTRY.register(adapter_cls)


def build_job_adapter(source) -> JobSourceAdapter:
    """Build an adapter only after all database and code policy gates pass."""
    adapter_cls = REGISTRY.get(source.adapter_key)
    if adapter_cls is None:
        raise UnknownJobAdapter(f"No job adapter registered for {source.adapter_key!r}")

    from crank.models.source import ApprovalState

    if source.approval_state == ApprovalState.BLOCKED:
        raise JobSourceBlocked(f"Job source {source.name!r} is blocked")
    if source.approval_state != ApprovalState.APPROVED:
        raise JobSourceNotApproved(f"Job source {source.name!r} is not approved")
    if not source.enabled:
        raise JobSourceDisabled(f"Job source {source.name!r} is disabled")
    try:
        validate_job_url(source.base_url, allow_hosts=source.allowed_hosts())
    except Exception as exc:
        if isinstance(exc, UnapprovedJobSource):
            raise
        raise UnapprovedJobSource(f"Job source {source.name!r}: {exc}") from exc
    return adapter_cls(source)


__all__ = ["REGISTRY", "JobAdapterRegistry", "register_job_adapter", "build_job_adapter"]
