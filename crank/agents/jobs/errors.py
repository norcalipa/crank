# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Errors raised at the external job-source adapter boundary."""


class JobAdapterError(Exception):
    """Base class for job source adapter failures."""


class JobSchemaError(JobAdapterError):
    """A raw listing or query violates the adapter contract."""


class UnknownJobAdapter(JobAdapterError):
    """The source's adapter key is not registered in code."""


class JobSourceNotApproved(JobAdapterError):
    """The source has not received operator approval."""


class JobSourceBlocked(JobAdapterError):
    """The source is explicitly blocked."""


class JobSourceDisabled(JobAdapterError):
    """The source is approved but not enabled by an operator."""


class UnapprovedJobSource(JobAdapterError):
    """A source or listing URL is outside the code-owned host allowlist."""
