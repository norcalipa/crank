# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""User-facing help and privacy documentation views.

These views render plain-language pages that explain what the agent stores,
user capabilities/limitations, preference and conversation storage, retention,
export/reset/delete, and how to report problems.  Views are public (no login
required) so prospective users can review privacy practices before signing in.
"""
from django.views.generic import TemplateView


class HelpView(TemplateView):
    """Main help landing page linking to privacy, FAQ, and support paths."""

    template_name = "crank/help/help.html"


class PrivacyView(TemplateView):
    """Privacy notice: what is stored, retention, export, reset, and deletion."""

    template_name = "crank/help/privacy.html"
