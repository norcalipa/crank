# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Phase 1: bounded job-search conversation orchestration.

This package implements the provider-independent orchestration service for the
career-advisor flow. It combines a versioned system prompt, a bounded set of
server-controlled organization/score tools, model-context construction with
deterministic truncation, and a strict schema gate on model output before any
preference patch touches persistence.

Security model
--------------
* The model can only learn about organizations that the ``org_datasource`` /
  ``score_datasource`` ports return. Those ports are injected and, by default,
  query only active/public ``Organization`` rows with fixed result limits.
* The model output is validated against ``AssistantCompletion``. Organization
  IDs cited by the model that are **not** a subset of the server-controlled
  catalog are rejected up front (hallucinated IDs) and never persisted.
* Preference updates are delegated to an injected ``PreferenceService`` port
  (issue #306). Malformed patches raise ``InvalidPreferencePatchError`` and
  ``apply_patch`` is never invoked, so nothing is persisted.
* The package exposes no way for the model to reach arbitrary models, SQL,
  files, hosts, or URLs.
"""
from crank.agents.job_search.errors import (
    CostLimitError,
    InvalidJobListingReferenceError,
    InvalidModelOutputError,
    InvalidOrganizationReferenceError,
    InvalidPreferencePatchError,
    JobSearchError,
    ProviderError,
    ProviderTimeoutError,
)
from crank.agents.job_search.service import (
    JobSearchOrchestrator,
    OrchestratorResult,
)

__all__ = [
    "CostLimitError",
    "InvalidJobListingReferenceError",
    "InvalidModelOutputError",
    "InvalidOrganizationReferenceError",
    "InvalidPreferencePatchError",
    "JobSearchError",
    "JobSearchOrchestrator",
    "OrchestratorResult",
    "ProviderError",
    "ProviderTimeoutError",
]
