# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Test helper for the signed preference tokens (issue #539)."""
from crank.services.preferences import sign_token


def signed(token, user, kind="proposal"):
    """Return ``token`` with the ``sig`` the server would have issued for it.

    Tests that hand-build a token (to set a precondition, an origin or a
    scope) call this last, so the edit under test is the only thing that
    differs from a server-issued token.
    """
    return sign_token(kind, token, user)
