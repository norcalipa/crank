# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# crank/forms.py
from django import forms


class OrganizationFilterForm(forms.Form):
    accelerated_vesting = forms.BooleanField(required=False, label='Show only companies with first vesting in < 1 year')

    def __init__(self, *args, **kwargs):
        initial = kwargs.get('initial', {})
        if 'request' in kwargs:
            self.request = kwargs.pop('request')
            initial['accelerated_vesting'] = self.request.session.get('accelerated_vesting', False)
        kwargs['initial'] = initial
        super().__init__(*args, **kwargs)

    def clean_accelerated_vesting(self):
        current_value = self.request.session.get('accelerated_vesting', False)
        new_value = not current_value
        self.request.session['accelerated_vesting'] = new_value
        return new_value