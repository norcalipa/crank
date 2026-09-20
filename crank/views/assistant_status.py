# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Advisory, user-safe assistant availability status (issue #457).

This endpoint answers one question for the chat UI: *should the user expect
the job-search assistant to respond right now?* It exists so users never
knowingly submit a message into a dead assistant; the UI offers useful
actions instead of a futile send.

Contract:

* ``GET`` only, anonymous-accessible, CSRF-exempt (safe read-only GET).
* The response is a bounded JSON envelope::

    {"state": "<signed_out|replies_disabled|temporarily_unavailable|"
              "inventory_unavailable|refreshing|ready>",
     "actions": ["<slug>", ...],
     "checked_at": "<iso8601>"}

  ``state`` is a closed enum and ``actions`` are opaque slugs the client maps
  to links/copy. HTTP is 200 for every state — the state is data, not an
  error.
* **Leak-proof by construction.** The response never contains the provider
  class or provider name, secret-presence flags, ``capability_report()``
  issue strings, table/schema names, stack traces, or private account data.
  The capability report is deliberately *not* consulted here at all;
  classification derives only from settings booleans, fail-closed provider
  construction, the latest ``job_pipeline`` ``AgentRun`` status, and a
  bounded active-listing existence query — all server-controlled, no
  network, no credential reads.
* **Advisory only.** A stale or failed status response can never gate or
  ungate the actual send path. :mod:`crank.views.job_search` is unchanged and
  remains authoritative: its POST error envelopes (503 ``assistant_unavailable``
  and friends) are the durable contract when the status check is wrong.
* **Cache-bounded.** Results are cached in the Django cache with a TTL
  clamped to ``<= 60`` seconds and keyed only on the request's auth state
  (anonymous vs authenticated). No per-user data enters the key or payload;
  staff receive exactly the same public envelope as ordinary users.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from crank.agents.job_search.demo import AssistantUnavailable, _build_provider
from crank.checks import is_non_dev_environment
from crank.models import AgentRun, JobListing, JobSourceCatalog

logger = logging.getLogger("crank.assistant_status")

# -- State enum (classification precedence order) ----------------------------

#: Anonymous requester; the chat requires login, so nothing else is checked.
SIGNED_OUT = "signed_out"
#: The assistant flag is off, or the offline demo/unset provider would serve a
#: non-dev deployment per ``crank.checks.is_non_dev_environment()``.
REPLIES_DISABLED = "replies_disabled"
#: The capability is enabled but fail-closed provider construction raised
#: ``AssistantUnavailable`` (e.g. missing LLM configuration at runtime).
TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
#: A ``job_pipeline`` ``AgentRun`` is currently RUNNING/PENDING (inventory may
#: be mid-refresh; the assistant itself is up).
REFRESHING = "refreshing"
#: No active ``JobListing`` rows exist under approved+enabled sources.
INVENTORY_UNAVAILABLE = "inventory_unavailable"
#: Everything checked out; the send path is expected to work.
READY = "ready"

# -- Public action slugs (opaque vocabulary; the client maps slugs to links) --

#: A link to the organization rankings page stays useful in every state.
ACTION_BROWSE_RANKINGS = "browse_rankings"
#: Re-checking the status is meaningful (transient/pending conditions).
ACTION_RETRY = "retry"

#: Per-state action slugs, in a stable, bounded order.
ACTIONS = {
    SIGNED_OUT: (),
    REPLIES_DISABLED: (ACTION_BROWSE_RANKINGS,),
    TEMPORARILY_UNAVAILABLE: (ACTION_RETRY,),
    REFRESHING: (ACTION_RETRY,),
    INVENTORY_UNAVAILABLE: (ACTION_BROWSE_RANKINGS,),
    READY: (),
}

#: Hard upper bound for the cache TTL regardless of configuration (issue #457
#: AC-3: the status is advisory and must never serve long-stale answers).
MAX_CACHE_SECONDS = 60
DEFAULT_CACHE_SECONDS = 30

#: Cache key prefix; bumped on envelope-shape changes to invalidate cleanly.
_CACHE_KEY_PREFIX = "assistant_status:v1"


def cache_seconds() -> int:
    """Return the configured cache TTL clamped to ``[0, MAX_CACHE_SECONDS]``.

    An unset or non-integer setting falls back to ``DEFAULT_CACHE_SECONDS``;
    values above the cap are clamped down; zero disables caching entirely
    (Django treats ``timeout=0`` as *never expire*, so zero means skip).
    """
    raw = getattr(settings, "ASSISTANT_STATUS_CACHE_SECONDS", DEFAULT_CACHE_SECONDS)
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CACHE_SECONDS
    if seconds <= 0:
        return 0
    return min(seconds, MAX_CACHE_SECONDS)


def _inventory_ready() -> bool:
    """Return True when at least one active listing has an approved+enabled source.

    A single bounded ``EXISTS``-shaped query; mirrors the read-only, no-network
    discipline of :mod:`crank.services.inventory_health`. A blocked, pending,
    or disabled source never counts, and neither do closed/expired listings.
    """
    return JobListing.objects.filter(
        status=JobListing.Status.ACTIVE,
        source__approval_state=JobSourceCatalog.ApprovalState.APPROVED,
        source__enabled=True,
    ).exists()


def _pipeline_refreshing() -> bool:
    """Return True while a job_pipeline AgentRun is PENDING or RUNNING."""
    return AgentRun.objects.filter(
        run_type=AgentRun.RunType.JOB_PIPELINE,
        status__in=(AgentRun.Status.PENDING, AgentRun.Status.RUNNING),
    ).exists()


def _classify_authenticated_state() -> str:
    """Classify the assistant state for an authenticated requester.

    Precedence (documented and tested, issue #457 AC-2):
    ``replies_disabled`` > ``temporarily_unavailable`` > ``refreshing`` >
    ``inventory_unavailable`` > ``ready``. ``signed_out`` is decided by the
    caller (it depends on the request, not the configuration).
    """
    # Demo/unset provider in a non-dev environment is a policy failure, not a
    # transient outage: replies are disabled until an operator reconfigures.
    provider_name = (
        getattr(settings, "JOB_SEARCH_PROVIDER", "demo") or "demo"
    ).strip().lower()
    if provider_name in ("", "demo") and is_non_dev_environment():
        return REPLIES_DISABLED
    # Feature flag off (a non-demo provider): the assistant is administratively
    # disabled. The demo provider is the dev/test double and serves in dev
    # regardless of this flag — its send path never consults it — so the flag
    # only gates a non-demo (orchestrator) configuration.
    flag_off = not getattr(settings, "INTERACTIVE_AGENT_ENABLED", False)
    if flag_off and provider_name not in ("", "demo"):
        return REPLIES_DISABLED
    # Ask the (already fail-closed) provider factory. Construction is
    # config-only and never performs network or credential reads; the
    # exception text is consumed here and never serialized. A successful
    # construction means the provider can serve right now (the demo always
    # can in dev; the orchestrator only when it is enabled and configured).
    try:
        _build_provider()
    except AssistantUnavailable:
        return TEMPORARILY_UNAVAILABLE
    except Exception:
        # Defensive: an unexpected construction failure is still "unavailable
        # right now" from the user's perspective; log server-side only.
        logger.warning("assistant status: provider construction failed", exc_info=True)
        return TEMPORARILY_UNAVAILABLE
    # Pipeline work in flight can leave the inventory mid-refresh.
    if _pipeline_refreshing():
        return REFRESHING
    if not _inventory_ready():
        return INVENTORY_UNAVAILABLE
    return READY


def _classification(request) -> dict:
    """Return the cached-or-fresh ``{"state", "checked_at"}`` for a request.

    Cached under a key that carries only the auth state (anonymous vs
    authenticated); every authenticated user and every staff member share the
    same public envelope.
    """
    auth_key = "auth" if request.user.is_authenticated else "anon"
    key = f"{_CACHE_KEY_PREFIX}:{auth_key}"
    seconds = cache_seconds()
    if seconds > 0:
        cached = cache.get(key)
        if cached is not None:
            return cached
    if auth_key == "anon":
        state = SIGNED_OUT
    else:
        state = _classify_authenticated_state()
    result = {"state": state, "checked_at": timezone.now().isoformat()}
    if seconds > 0:
        cache.set(key, result, timeout=seconds)
    return result


@require_GET
def assistant_status(request):
    """Report advisory, user-safe assistant availability (issue #457).

    Advisory, not proof: a ``ready`` response does not guarantee a working
    provider or a populated inventory at send time; the POST path in
    :mod:`crank.views.job_search` remains authoritative and its error
    envelopes are unchanged. Staff-only diagnostics
    (:mod:`crank.views.release_diagnostics`, ``capability_report()``) stay
    staff-only — this endpoint never carries them.
    """
    classification = _classification(request)
    return JsonResponse(
        {
            "state": classification["state"],
            "actions": list(ACTIONS.get(classification["state"], ())),
            "checked_at": classification["checked_at"],
        }
    )


__all__ = [
    "ACTION_BROWSE_RANKINGS",
    "ACTION_RETRY",
    "ACTIONS",
    "DEFAULT_CACHE_SECONDS",
    "INVENTORY_UNAVAILABLE",
    "MAX_CACHE_SECONDS",
    "READY",
    "REFRESHING",
    "REPLIES_DISABLED",
    "SIGNED_OUT",
    "TEMPORARILY_UNAVAILABLE",
    "assistant_status",
    "cache_seconds",
]
