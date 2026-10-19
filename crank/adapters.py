# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Custom allauth adapters.

django-allauth's default ``SocialAccountAdapter.on_authentication_error`` is a
no-op, so the underlying exception that triggers the stock "Third-Party Login
Failure" page (``OAuth2Error``, ``requests.RequestException``,
``ProviderException``, ``PermissionDenied``) is silently swallowed and never
appears in the logs. That makes the callback failure impossible to diagnose in
production. This adapter logs the provider, error code, and exception so the
real cause (token-exchange rejection, egress failure, clock skew, ...) is
visible without surfacing it to the user.
"""
import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from crank.client_address import client_address

logger = logging.getLogger("users")


class AccountAdapter(DefaultAccountAdapter):
    """Key allauth's per-address rate limits on the trusted client address.

    allauth's default reads the leftmost ``X-Forwarded-For``, which a client
    behind the edge chooses. When ``client_address()`` identifies the request
    its bucket is used; otherwise allauth's own value is returned unchanged.
    """

    def get_client_ip(self, request):
        address = client_address(request)
        if address is not None:
            return address.bucket
        return super().get_client_ip(request)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def on_authentication_error(
        self,
        request,
        provider,
        error=None,
        exception=None,
        extra_context=None,
    ):
        logger.warning(
            "socialaccount authentication error: provider=%s path=%s "
            "error=%r exception=%r",
            getattr(provider, "id", provider),
            request.path,
            error,
            exception,
            exc_info=exception,
        )
        return super().on_authentication_error(
            request,
            provider,
            error=error,
            exception=exception,
            extra_context=extra_context,
        )
