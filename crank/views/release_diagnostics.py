# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Staff-only release diagnostics view."""

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render

from crank.release import diagnostics


@staff_member_required
def release_diagnostics(request):
    """Render the staff-only release diagnostics surface."""
    return render(
        request,
        "crank/release_diagnostics.html",
        {"diagnostics": diagnostics()},
    )
