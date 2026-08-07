# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Code-owned source-adapter registry and factory (issue #311).

Adapters are registered in code (never dynamically imported from database
supplied paths or URLs). :func:`build_adapter` enforces that only sources which
are approved, operator-enabled, on the code-owned base-domain allowlist, and
whose ``adapter_key`` has a registered implementation can be instantiated --
rejecting unknown, disabled, pending, and blocked sources.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from django.db.models import Model

from crank.agents.sources.base import (
    SourceAdapter,
    SourceBlocked,
    SourceDisabled,
    SourceNotApproved,
    UnknownSourceAdapter,
    UnapprovedBaseUrl,
    validate_source_base_url,
    RawScoreObservation,
    ObservationValidationError,
)

T = TypeVar("T", bound=Model)
AdapterT = TypeVar("AdapterT", bound=SourceAdapter)


class SourceRegistry(Generic[AdapterT]):
    """In-code registry mapping adapter keys to adapter classes."""

    def __init__(self) -> None:
        self._adapters: dict[str, type[AdapterT]] = {}

    def register(self, adapter_cls: type[AdapterT]) -> type[AdapterT]:
        key = getattr(adapter_cls, "key", None)
        if not key or not isinstance(key, str):
            raise ValueError(
                f"Adapter {adapter_cls.__name__} must define a non-empty `key`"
            )
        if key in self._adapters:
            raise ValueError(f"Adapter key {key!r} is already registered")
        self._adapters[key.lower()] = adapter_cls
        return adapter_cls

    def get(self, key: str) -> type[AdapterT] | None:
        return self._adapters.get((key or "").lower())

    def keys(self) -> list[str]:
        return sorted(self._adapters)

    def __contains__(self, key: str) -> bool:
        return (key or "").lower() in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)


#: Process-wide registry. Tests may register fake adapters under distinct keys.
REGISTRY: SourceRegistry[SourceAdapter] = SourceRegistry()


def register_source_adapter(adapter_cls: type[SourceAdapter]) -> type[SourceAdapter]:
    """Decorator registering an adapter class in the global registry."""
    return REGISTRY.register(adapter_cls)


def build_adapter(source) -> SourceAdapter:
    """Instantiate the adapter for ``source`` or raise a typed rejection.

    Order of checks: registered key -> approval state (blocked/pending) ->
    operator enabled -> base domain allowlist. Only sources that pass every
    gate can be instantiated.
    """
    adapter_cls = REGISTRY.get(source.adapter_key)
    if adapter_cls is None:
        raise UnknownSourceAdapter(
            f"No adapter registered for source key {source.adapter_key!r}"
        )

    from crank.models.source import ApprovalState

    if source.approval_state == ApprovalState.BLOCKED:
        raise SourceBlocked(f"Source {source.name!r} is blocked")
    if source.approval_state != ApprovalState.APPROVED:
        raise SourceNotApproved(
            f"Source {source.name!r} is not yet approved (state="
            f"{source.approval_state!r})"
        )
    if not source.enabled:
        raise SourceDisabled(f"Source {source.name!r} is disabled")

    try:
        validate_source_base_url(source.base_url)
    except UnapprovedBaseUrl as exc:
        raise UnapprovedBaseUrl(
            f"Source {source.name!r}: {exc}"
        ) from exc

    return adapter_cls(source)  # type: ignore[call-arg]


def validate_observation_for_source(observation: RawScoreObservation, source) -> None:
    """Enforce source-level policy on a typed observation.

    Checks the observation's source URL is on the allowlist and that its score
    type matches one of the source's recorded capabilities. Adapters should run
    this over every observation they produce before returning it.
    """
    try:
        validate_source_base_url(observation.source_url)
    except UnapprovedBaseUrl as exc:
        raise ObservationValidationError(
            f"Observation source_url not allowed: {exc}"
        ) from exc
    if not source.supports_score_type(observation.score_type):
        raise ObservationValidationError(
            f"Score type {observation.score_type!r} is not a recorded capability "
            f"of source {source.name!r}"
        )
