# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from allauth.account.views import LogoutView as AllauthLogoutView
from django.shortcuts import redirect, render


class CustomLogoutView(AllauthLogoutView):
    """CSRF-checked logout (issue #470 review removed the exemption).

    The previous global ``csrf_exempt`` let any third-party page POST here and
    force-log-out a signed-in user. Logout is now a normal CSRF-protected
    POST: server-rendered shells embed a per-session token in the logout
    form, and the shared full-page-cached shell — which must stay token-free
    — submits through app-nav.js with the CSRF cookie guaranteed by the
    whoami endpoint.
    """

    def post(self, request, *args, **kwargs):
        _ = super().post(request, *args, **kwargs)
        return redirect('index')  # Redirect to home page after logout
