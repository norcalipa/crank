# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Score adapter contracts for Phase 2 (issue #311).

Code-owned adapter registry/factory plus the typed observation boundary between
external payloads and normalized application data. Concrete network adapters
are out of scope (issue #311); this package is the contract layer they build on.
"""

from crank.agents.sources.base import (  # noqa: F401
    RawScoreObservation,
    ObservationValidationError,
    SourceCatalogError,
    UnknownSourceAdapter,
    SourceNotApproved,
    SourceBlocked,
    SourceDisabled,
    UnapprovedBaseUrl,
    SourceAdapter,
    validate_source_base_url,
)
from crank.agents.sources.registry import (  # noqa: F401
    SourceRegistry,
    REGISTRY,
    register_source_adapter,
    build_adapter,
    validate_observation_for_source,
)

__all__ = [
    "RawScoreObservation",
    "ObservationValidationError",
    "SourceCatalogError",
    "UnknownSourceAdapter",
    "SourceNotApproved",
    "SourceBlocked",
    "SourceDisabled",
    "UnapprovedBaseUrl",
    "SourceAdapter",
    "validate_source_base_url",
    "SourceRegistry",
    "REGISTRY",
    "register_source_adapter",
    "build_adapter",
    "validate_observation_for_source",
]
