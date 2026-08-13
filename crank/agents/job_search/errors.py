# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Typed service errors for the job-search orchestration service.

These error types are the contract that callers (HTTP views, management
commands) can rely on. Timeout, provider failure, and cost-limit outcomes all
map to explicit subtypes of :class:`ProviderError`; malformed or untrusted
model output maps to subtypes of :class:`InvalidModelOutputError`.
"""


class JobSearchError(Exception):
    """Base class for all job-search orchestration errors."""


class ProviderError(JobSearchError):
    """The LLM provider failed to produce a usable completion."""


class ProviderTimeoutError(ProviderError):
    """The provider exceeded its enforcement/response timeout."""


class CostLimitError(ProviderError):
    """The provider call would exceed the configured token or cost limit."""


class InvalidModelOutputError(JobSearchError):
    """Model output failed schema validation and was rejected before use."""


class InvalidOrganizationReferenceError(InvalidModelOutputError):
    """The model cited an organization ID not returned by server-controlled tools.

    Cited organization IDs must be a strict subset of the IDs the bounded
    organization tools actually returned. Anything else is treated as a
    hallucinated reference and rejected without persistence.
    """


class InvalidPreferencePatchError(InvalidModelOutputError):
    """The model's preference patch is malformed or violates the preference schema."""


class InvalidScoreSummaryRowError(JobSearchError):
    """A score-summary datasource returned a malformed/untyped row.

    Score rows are server-controlled but injectable (and may be faked in
    tests), so a row missing or mistyping ``organization_id``, ``score_type``
    or ``avg_score`` is surfaced as this typed error instead of a bare
    ``KeyError`` during rendering.
    """


class InvalidJobListingReferenceError(InvalidModelOutputError):
    """The model cited a job-listing ID not returned by server-controlled tools.

    Cited listing IDs must be a strict subset of the IDs the bounded
    job-listing tools actually returned. Anything else is treated as a
    hallucinated reference and rejected without persistence.
    """
