# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Validation form for bounded company suggestions."""

from django import forms

from crank.models.company_request import CompanyRequest


class CompanyRequestForm(forms.ModelForm):
    class Meta:
        model = CompanyRequest
        fields = ["company_name", "website_url", "careers_url", "reason"]
        widgets = {
            "company_name": forms.TextInput(attrs={"maxlength": 100}),
            "website_url": forms.URLInput(attrs={"placeholder": "https://example.com"}),
            "careers_url": forms.URLInput(attrs={"placeholder": "https://example.com/careers"}),
            "reason": forms.Textarea(attrs={"maxlength": 500, "rows": 3}),
        }

    def clean_company_name(self):
        value = " ".join(self.cleaned_data["company_name"].split())
        if not value:
            raise forms.ValidationError("Enter a company name.")
        return value
