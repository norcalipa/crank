# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Authentication helpers used by CRank's browser-facing protected views.

Issue #465 removed the old ``login_required_with_expiry`` decorator, which
discarded the visitor's intended destination on every anonymous request to
``/chat/``. In its place this module offers:

* :func:`safe_next_url` / :func:`sign_in_url` — a validated ``next`` handoff
  through sign-in, built on Django's own open-redirect guard.
* :func:`visitor_state` plus :data:`AUTH_SEEN_COOKIE` /
  :func:`mark_authenticated_visit` — a way to tell a first-time visitor from
  one whose session merely expired, using a contentless marker cookie that
  outlives a flushed session (unlike anything stored in ``request.session``).
"""
from __future__ import annotations

from urllib.parse import unquote, urlencode

from django.conf import settings
from django.utils.http import url_has_allowed_host_and_scheme


SESSION_EXPIRED_MESSAGE = (
    "Your session has expired or you have been logged out. Please sign in again."
)

FIRST_VISIT_INTRO = (
    "Ask the CRank assistant about compensation, remote work, funding, and "
    "culture across our tracked companies. Sign in to save your conversation "
    "and get personalized job matches."
)

#: Non-identifying, long-lived cookie: it says only "this browser has signed
#: in at least once" and carries no account identity. It is never consulted
#: for authorization, only to distinguish a first visit from an expired
#: session in :func:`visitor_state`.
AUTH_SEEN_COOKIE = "crank_seen_auth"
AUTH_SEEN_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 5  # 5 years

#: Request attribute :class:`MarkAuthenticatedVisitMiddleware` checks to
#: decide whether to set :data:`AUTH_SEEN_COOKIE` on the outgoing response.
#: Split across a signal receiver and a middleware because
#: ``user_logged_in`` fires while the view is still running, before any
#: response (and therefore before anything can call ``set_cookie``) exists.
_SEEN_AUTH_REQUEST_FLAG = "_crank_mark_seen_auth"

#: Upper bound on percent-decoding rounds in :func:`_fully_decoded`. The
#: request stack decodes once, but a candidate can be double-encoded
#: (``/%2561ccounts/``) precisely to survive a single-decode comparison, so
#: normalization decodes to a fixed point instead. Bounded so a pathological
#: candidate cannot spin.
_MAX_DECODE_ROUNDS = 5

#: Path prefix the ``next`` handoff must never target: the login/logout
#: machinery itself.
_AUTH_PATH_PREFIX = "/accounts/"


def _fully_decoded(value: str) -> str:
    """Percent-decode ``value`` repeatedly until it stops changing.

    A single ``unquote`` leaves double-encoded forms intact, so
    ``/%2561ccounts/logout/`` would still read as an innocent path while the
    request stack turns it back into ``/accounts/logout/``. Decoding to a
    fixed point (bounded by :data:`_MAX_DECODE_ROUNDS`) compares what the
    stack will actually route.
    """
    decoded = value
    for _ in range(_MAX_DECODE_ROUNDS):
        once = unquote(decoded)
        if once == decoded:
            break
        decoded = once
    return decoded


def _targets_auth_machinery(value: str) -> bool:
    """Return whether ``value``'s path component targets ``/accounts/``.

    Checked against both the raw candidate and its fully decoded form:
    ``/accounts/logout/``, ``/%61ccounts/logout/`` and
    ``/accounts%2flogout/`` all reach the auth machinery once the request
    stack has decoded them, so comparing only the raw spelling (the
    pre-review behaviour) let the encoded variants through. Backslashes fold
    to ``/`` so a Windows-style separator cannot hide the prefix either.
    """
    for variant in (value, _fully_decoded(value)):
        path_only = variant.split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
        if path_only.startswith(_AUTH_PATH_PREFIX) or path_only == "/accounts":
            return True
    return False


def safe_next_url(request, candidate: str | None) -> str | None:
    """Return ``candidate`` if it is a safe same-origin redirect target.

    Rejects anything that is not a same-origin, same-scheme, root-relative
    path, plus any path under ``/accounts/`` (the login/logout machinery
    itself must never be a redirect target) — including percent-encoded and
    double-encoded spellings of that prefix. Returns ``None`` for every
    other candidate, including ``None`` or an empty string.
    """
    if not candidate:
        return None
    if not candidate.startswith("/"):
        return None
    decoded_candidate = _fully_decoded(candidate)
    if (
        decoded_candidate.startswith("//")
        or decoded_candidate.startswith("/\\")
        or "\r" in decoded_candidate
        or "\n" in decoded_candidate
    ):
        return None
    if not url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return None
    if _targets_auth_machinery(candidate):
        return None
    return candidate


def sign_in_url(request, next_url: str | None = None) -> str:
    """Build a login URL carrying a validated ``next`` query parameter.

    Any candidate that :func:`safe_next_url` rejects falls back to ``/`` so
    the emitted URL is always well-formed and never leaks a rejected value.
    """
    safe = safe_next_url(request, next_url) or "/"
    return f"{settings.LOGIN_URL}?{urlencode({'next': safe})}"


def visitor_state(request) -> str:
    """Classify the requester as ``authenticated``, ``session_expired``, or
    ``anonymous_first_visit``.

    The cookie is the only signal for the anonymous branches: it outlives a
    flushed session, so it is the sole way to tell "this browser has never
    signed in" apart from "this browser's session just expired or logged
    out".
    """
    if request.user.is_authenticated:
        return "authenticated"
    if request.COOKIES.get(AUTH_SEEN_COOKIE):
        return "session_expired"
    return "anonymous_first_visit"


def mark_authenticated_visit(sender, request, user, **kwargs) -> None:
    """``user_logged_in`` receiver: flag this request for the cookie.

    The signal fires before the view returns a response, so the cookie
    itself is set later by :class:`MarkAuthenticatedVisitMiddleware`.
    """
    setattr(request, _SEEN_AUTH_REQUEST_FLAG, True)


class MarkAuthenticatedVisitMiddleware:
    """Sets :data:`AUTH_SEEN_COOKIE` on responses to requests flagged by
    :func:`mark_authenticated_visit`.

    A plain middleware, not a signal receiver, because ``user_logged_in``
    has no response to attach a cookie to; this reads the flag the receiver
    left on ``request`` once a response exists.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(request, _SEEN_AUTH_REQUEST_FLAG, False):
            response.set_cookie(
                AUTH_SEEN_COOKIE,
                "1",
                max_age=AUTH_SEEN_COOKIE_MAX_AGE,
                httponly=True,
                samesite="Lax",
                secure=settings.SESSION_COOKIE_SECURE,
            )
        return response
