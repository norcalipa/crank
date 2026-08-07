# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Provider-independent gateway contract.

The orchestration service talks only to this interface. Concrete providers
(issue #304) translate provider-specific failures into :class:`ProviderError`,
:class:`ProviderTimeoutError`, and :class:`CostLimitError` subclasses and own
timeouts, retries, and token/cost ceilings. This keeps provider behavior behind
the gateway and the service testable with a fake gateway.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from crank.agents.job_search.errors import (
    CostLimitError,
    ProviderError,
    ProviderTimeoutError,
)

__all__ = [
    "CostLimitError",
    "GatewayResponse",
    "ModelRequest",
    "ProviderError",
    "ProviderGateway",
    "ProviderTimeoutError",
]


@dataclass(frozen=True)
class ModelRequest:
    """A single bounded completion request assembled by the orchestrator."""

    prompt_id: str
    system: str
    messages: List[Dict[str, str]]
    max_tokens: Optional[int] = None
    token_budget: Optional[int] = None


@dataclass(frozen=True)
class GatewayResponse:
    """A provider completion plus minimal usage counters for telemetry."""

    text: str
    usage: Dict[str, int] = field(default_factory=dict)

    @property
    def output_tokens(self) -> int:
        return int(self.usage.get("output_tokens", 0))


class ProviderGateway(ABC):
    """Minimal, provider-independent completion gateway."""

    @abstractmethod
    def complete(self, request: ModelRequest) -> GatewayResponse:
        """Return a completion.

        Must raise :class:`ProviderTimeoutError` on timeout and
        :class:`CostLimitError` when the request would exceed the configured
        token/cost ceiling. Any other provider failure raises
        :class:`ProviderError`.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release provider resources. Optional; idempotent."""

    def __enter__(self) -> "ProviderGateway":
        return self

    def __exit__(self, *exc) -> None:
        self.close()