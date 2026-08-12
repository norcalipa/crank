# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Versioned, immutable configuration for deterministic job ranking."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Mapping


FACTOR_NAMES = (
    "work_location",
    "geography",
    "compensation",
    "industry",
    "funding_stage",
    "culture",
    "vesting",
    "organization_scores",
)


@dataclass(frozen=True)
class RankingConfig:
    """The complete versioned policy used to calculate a match score.

    Weights are fractions of ``max_score``.  The default weights sum to one;
    custom configurations may use a larger total, but the final score is still
    clamped to ``max_score``.
    """

    version: str
    weights: dict[str, float] = field(default_factory=dict)
    max_score: float = 100.0
    missing_data_penalty: float = 0.0

    def __post_init__(self) -> None:
        if not self.version or not isinstance(self.version, str):
            raise ValueError("RankingConfig.version must be a non-empty string")
        if not isfinite(float(self.max_score)) or self.max_score <= 0:
            raise ValueError("RankingConfig.max_score must be positive and finite")
        if not isfinite(float(self.missing_data_penalty)):
            raise ValueError("RankingConfig.missing_data_penalty must be finite")
        unknown = set(self.weights) - set(FACTOR_NAMES)
        if unknown:
            raise ValueError(f"Unknown ranking factors: {sorted(unknown)!r}")
        normalized = dict(self.weights)
        for factor, weight in normalized.items():
            if not isfinite(float(weight)) or weight < 0:
                raise ValueError(f"Weight for {factor!r} must be finite and non-negative")
        object.__setattr__(self, "weights", normalized)


DEFAULT_CONFIG = RankingConfig(
    version="1.0.0",
    weights={factor: 1.0 / len(FACTOR_NAMES) for factor in FACTOR_NAMES},
    max_score=100.0,
    missing_data_penalty=0.0,
)

_CONFIGS: Mapping[str, RankingConfig] = MappingProxyType(
    {DEFAULT_CONFIG.version: DEFAULT_CONFIG}
)


def config_for_version(version: str) -> RankingConfig:
    """Return the registered ranking policy for ``version``.

    Unknown versions fail closed.  A caller must explicitly register a new
    policy rather than silently replaying a different ranking algorithm.
    """

    try:
        return _CONFIGS[version]
    except KeyError as exc:
        raise ValueError(f"Unknown ranking configuration version: {version!r}") from exc


__all__ = ["DEFAULT_CONFIG", "FACTOR_NAMES", "RankingConfig", "config_for_version"]
