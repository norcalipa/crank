# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Authentication helpers used by CRank's browser-facing protected views."""
from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect


SESSION_EXPIRED_MESSAGE = (
    "Your session has expired or you have been logged out. Please sign in again."
)


def login_required_with_expiry(view_func):
    """Send anonymous browser requests home with a one-time session message.

    Unlike Django's default decorator, this deliberately does not preserve the
    protected URL in a ``next`` parameter. A stale session must not send a user
    back to a protected page before a fresh authentication succeeds.
    """

    @wraps(view_func)
    def wrapped_view(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view_func(request, *args, **kwargs)
        messages.warning(request, SESSION_EXPIRED_MESSAGE)
        return redirect("index")

    return wrapped_view
