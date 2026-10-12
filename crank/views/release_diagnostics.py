# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Staff-only release diagnostics view."""

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render

from crank.client_address import client_address, effective_source
from crank.release import diagnostics

ADDRESS_OFF = "off (no address is read)"
ADDRESS_NOT_IDENTIFIED = "on, not identified"


@staff_member_required
def release_diagnostics(request):
    """Render the staff-only release diagnostics surface."""
    address = client_address(request)
    if address:
        address_text = address.bucket
    elif effective_source():
        address_text = ADDRESS_NOT_IDENTIFIED
    else:
        address_text = ADDRESS_OFF
    return render(
        request,
        "crank/release_diagnostics.html",
        {
            "diagnostics": diagnostics(),
            # Shown to the viewing staff member only; never stored or logged.
            "client_address_text": address_text,
        },
    )
