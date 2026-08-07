# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Phase 2: source catalog and adapters for gathering raw external rating observations."""

from crank.agents.sources.base import (
    ObservationValidationError,
    RawScoreObservation as BaseRawScoreObservation,
    SourceAdapter as BaseSourceAdapter,
    SourceBlocked,
    SourceCatalogError,
    SourceDisabled,
    SourceNotApproved,
    UnapprovedBaseUrl,
    UnknownSourceAdapter,
    validate_source_base_url,
)
from crank.agents.sources.contract import (
    RawScoreObservation,
    SourceAdapter,
    SourceQuery,
    SourceResult,
)
from crank.agents.sources.registry import (
    REGISTRY,
    SourceRegistry,
    build_adapter,
    register_source_adapter,
    validate_observation_for_source,
)
from crank.agents.sources.yelp import YelpSourceAdapter

__all__ = [
    "RawScoreObservation",
    "BaseRawScoreObservation",
    "ObservationValidationError",
    "SourceCatalogError",
    "UnknownSourceAdapter",
    "SourceNotApproved",
    "SourceBlocked",
    "SourceDisabled",
    "UnapprovedBaseUrl",
    "SourceAdapter",
    "BaseSourceAdapter",
    "validate_source_base_url",
    "SourceRegistry",
    "REGISTRY",
    "register_source_adapter",
    "build_adapter",
    "validate_observation_for_source",
    "SourceQuery",
    "SourceResult",
    "YelpSourceAdapter",
]
