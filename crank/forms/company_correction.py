# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Validation form for bounded company-fact corrections."""

from django import forms

from crank.models.company_correction import CompanyCorrection


class CompanyCorrectionForm(forms.ModelForm):
    """Validates the user-controlled fields; the view supplies the rest.

    Model ``clean()`` owns normalization (whitespace, URL policy, scope rules).
    """

    class Meta:
        model = CompanyCorrection
        fields = [
            "field_key",
            "proposed_value",
            "evidence_url",
            "scope_level",
            "scope_value",
            "note",
            "idempotency_key",
        ]
