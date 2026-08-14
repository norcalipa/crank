# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""App config for the crank application.

Registers the deployment-safety system checks (issue #397). Referencing this
config (``crank.apps.CrankConfig``) in ``INSTALLED_APPS`` ensures ``ready()``
runs and the checks are available to ``manage.py check`` and at startup.
"""
from django.apps import AppConfig


class CrankConfig(AppConfig):
    """Primary application config; registers system checks."""

    name = "crank"
    verbose_name = "CRank"

    def ready(self) -> None:
        """Import and register system checks once Django is set up."""
        # Importing the module triggers ``@register()`` on the check functions.
        from crank import checks  # noqa: F401
