# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Django system checks for deployment-safety issues (issue #397).

The check here is deliberately a *warning* (not an error): the demo provider is
a legitimate offline/test choice, so a production deployment that still points
``JOB_SEARCH_PROVIDER`` at it should be surfaced loudly rather than block boot.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning, register

#: Environments that are treated as non-dev regardless of DEBUG.
_NON_DEV_ENVS = frozenset({"prod", "staging"})


def is_non_dev_environment() -> bool:
    """Return True when the configured environment is not a dev one.

    The crank package selects its settings module from the explicit ``ENV``
    env var (``prod``/``staging``) and otherwise defaults to local dev
    settings. ``ENV`` is the authoritative deployment selector and does not
    move during test runs (Django forces ``DEBUG`` off under the test runner,
    so DEBUG is not a reliable dev signal here).
    """
    env = (getattr(settings, "ENV", "") or "").strip().lower()
    return env in _NON_DEV_ENVS


@register()
def check_job_search_provider(app_configs=None, **kwargs) -> list[Warning]:
    """Warn when ``JOB_SEARCH_PROVIDER=demo`` in a non-dev environment.

    The demo provider is an offline simulator with no real LLM or server data
    grounding; if it leaked into a production deployment the assistant would
    appear to work while never producing citations or result cards. Surface
    that loudly at system-check time.
    """
    provider = (getattr(settings, "JOB_SEARCH_PROVIDER", "") or "").strip().lower()
    if provider == "demo" and is_non_dev_environment():
        return [
            Warning(
                "JOB_SEARCH_PROVIDER is 'demo' in a non-dev environment.",
                hint=(
                    "Set JOB_SEARCH_PROVIDER=orchestrator (with a configured "
                    "LLM gateway) so the assistant serves real, server-bound "
                    "recommendations instead of the simulator. The demo "
                    "provider is an offline test double and is not appropriate "
                    "for a production deployment."
                ),
                obj=settings,
                id="crank.W001",
            )
        ]
    return []


__all__ = [
    "check_job_search_provider",
    "is_non_dev_environment",
]
