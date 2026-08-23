# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Production capability configuration contract (issue #440).

This module defines the single, documented, environment-backed production
capability contract shared across the web Deployment, migration Job, and
scheduled agent Jobs. It provides fail-closed validation: an enabled
capability that is missing required configuration is reported as a violation
so Kubernetes readiness probes and Django system checks can prevent rollout
or serving traffic before the misconfiguration reaches users.

All functions return **safe, non-secret** values only. Secret presence is
reduced to a boolean (``has_*_key``); actual key values are never logged,
serialized, or rendered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from django.conf import settings

# ---------------------------------------------------------------------------
# Capability contract constants
# ---------------------------------------------------------------------------

#: Capabilities that have fail-closed validation rules.
CAPABILITIES: tuple[str, ...] = (
    "interactive_agent",
    "job_pipeline",
    "crawl",
)

#: Source-provider secret keys that the contract validates as present/absent.
#: The actual values are never read, logged, or serialized by this module.
SOURCE_SECRET_KEYS: tuple[str, ...] = (
    "YELP_API_KEY",
    "USAJOBS_AUTH_KEY",
    "FIRECRAWL_API_KEY",
)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityStatus:
    """Safe, non-secret capability status for diagnostics and readiness.

    ``issues`` is a list of human-readable, non-secret validation messages.
    An empty list means the capability is correctly configured (either
    disabled, or enabled with all required settings).
    """

    name: str
    enabled: bool
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the capability has no configuration issues."""
        return not self.issues


@dataclass(frozen=True)
class CapabilityReport:
    """Aggregate report of all capability statuses."""

    capabilities: list[CapabilityStatus]
    config_version: str

    @property
    def all_ok(self) -> bool:
        """True when every capability is correctly configured."""
        return all(c.ok for c in self.capabilities)

    def to_dict(self) -> dict:
        """Serialize to a safe dict for JSON responses. Never includes secrets."""
        issues = capability_issues()
        return {
            "config_version": self.config_version,
            "capabilities": [
                {
                    "name": c.name,
                    "enabled": c.enabled,
                    "ok": c.ok,
                    "issues": list(c.issues),
                }
                for c in self.capabilities
            ],
            "capability_issues": issues,
            "all_ok": self.all_ok,
        }


# ---------------------------------------------------------------------------
# Individual capability validators
# ---------------------------------------------------------------------------


def _interactive_agent_status() -> CapabilityStatus:
    """Validate the interactive agent capability (fail closed).

    Enabling chat requires:
      - ``INTERACTIVE_AGENT_ENABLED=true``
      - ``LLM_PROVIDER`` set to a non-empty provider class
      - ``LLM_MODEL`` set to a non-empty model identifier
      - ``LLM_API_KEY`` present (non-empty) in the environment

    Missing any of these while the feature flag is on is a configuration
    error that must prevent serving chat traffic.
    """
    enabled = bool(getattr(settings, "INTERACTIVE_AGENT_ENABLED", False))
    if not enabled:
        return CapabilityStatus(name="interactive_agent", enabled=False)

    issues: list[str] = []
    provider = (getattr(settings, "LLM_PROVIDER", "") or "").strip()
    model = (getattr(settings, "LLM_MODEL", "") or "").strip()
    has_api_key = bool((getattr(settings, "LLM_API_KEY", "") or "").strip())

    if not provider:
        issues.append("LLM_PROVIDER is not set; no provider will be built")
    if not model:
        issues.append("LLM_MODEL is not set; provider requires a model identifier")
    if not has_api_key:
        issues.append("LLM_API_KEY is missing; provider requires a secret key")

    return CapabilityStatus(name="interactive_agent", enabled=True, issues=issues)


def _job_pipeline_status() -> CapabilityStatus:
    """Validate the job pipeline capability (fail closed).

    Enabling the job pipeline requires:
      - ``JOB_PIPELINE_ENABLED=true``
      - ``AGENT_RUN_ENABLED=true`` (the master switch for scheduled work)

    Source credentials are validated per-source at runtime through the
    SourceCatalog/JobSourceCatalog approval flow; this contract only checks
    the top-level orchestration flags.
    """
    enabled = bool(getattr(settings, "JOB_PIPELINE_ENABLED", False))
    if not enabled:
        return CapabilityStatus(name="job_pipeline", enabled=False)

    issues: list[str] = []
    if not bool(getattr(settings, "AGENT_RUN_ENABLED", False)):
        issues.append(
            "AGENT_RUN_ENABLED is false; job pipeline requires the master "
            "agent switch to be enabled"
        )

    return CapabilityStatus(name="job_pipeline", enabled=True, issues=issues)


def _crawl_status() -> CapabilityStatus:
    """Validate the crawl scheduling capability (fail closed).

    Enabling crawl scheduling requires:
      - ``CRAWL_CRON_ENABLED=true``
      - ``AGENT_RUN_ENABLED=true`` (the master switch for scheduled work)
    """
    enabled = bool(getattr(settings, "CRAWL_CRON_ENABLED", False))
    if not enabled:
        return CapabilityStatus(name="crawl", enabled=False)

    issues: list[str] = []
    if not bool(getattr(settings, "AGENT_RUN_ENABLED", False)):
        issues.append(
            "AGENT_RUN_ENABLED is false; crawl scheduling requires the "
            "master agent switch to be enabled"
        )

    return CapabilityStatus(name="crawl", enabled=True, issues=issues)


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------

#: Config version is a monotonic integer bumped when the contract changes.
#: Operators and diagnostics use this to detect manifest/config version drift.
CONFIG_VERSION = "1"


def capability_report() -> CapabilityReport:
    """Build the full capability report for readiness and diagnostics.

    Returns a :class:`CapabilityReport` with one :class:`CapabilityStatus`
    per capability. All values are safe booleans and non-secret strings.
    """
    return CapabilityReport(
        capabilities=[
            _interactive_agent_status(),
            _job_pipeline_status(),
            _crawl_status(),
        ],
        config_version=CONFIG_VERSION,
    )


def capability_issues() -> list[str]:
    """Return a flat list of all capability configuration issues.

    Empty list means all capabilities are correctly configured (either
    disabled or enabled with all required settings).
    """
    report = capability_report()
    return [
        f"{c.name}: {issue}"
        for c in report.capabilities
        for issue in c.issues
    ]


__all__ = [
    "CAPABILITIES",
    "CONFIG_VERSION",
    "CapabilityReport",
    "CapabilityStatus",
    "SOURCE_SECRET_KEYS",
    "capability_issues",
    "capability_report",
]
