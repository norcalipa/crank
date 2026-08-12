# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Source-neutral job ingestion contracts."""

from crank.agents.jobs.base import JobSourceAdapter, JobSourceQuery, JobSourceResult, RawJobListing
from crank.agents.jobs.registry import build_job_adapter, register_job_adapter
from crank.agents.jobs.usajobs import USAJobsAdapter, USAJobsSourceAdapter, USAJOBSAdapter
from crank.agents.jobs.firecrawl import FirecrawlCareersAdapter, FirecrawlClient

__all__ = [
    "JobSourceAdapter",
    "JobSourceQuery",
    "JobSourceResult",
    "RawJobListing",
    "build_job_adapter",
    "register_job_adapter",
    "USAJobsAdapter",
    "USAJobsSourceAdapter",
    "USAJOBSAdapter",
    "FirecrawlCareersAdapter",
    "FirecrawlClient",
]
