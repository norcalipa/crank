# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Shared empty-state model for job and recommendation surfaces.

Both the chat transport and the job-match API use this module to derive a
single, canonical ``EmptyState`` so the UI wording is consistent everywhere.

The module never exposes internal errors, credentials, or sensitive source
details.  Staff-only fields are marked ``staff_only`` and the API view is
responsible for stripping them for non-staff users.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional

from django.utils import timezone

from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.preference import UserPreference


# A user preference document is considered "empty" when every section is at
# its default.  We check a few representative fields rather than deep-comparing
# the entire document so the check stays cheap and resilient to schema bumps.
def _preferences_are_default(pref_doc: dict) -> bool:
    """Return True when the preference document is effectively empty."""
    if not pref_doc:
        return True
    comp = pref_doc.get("compensation", {})
    if comp.get("minimum_salary") is not None or comp.get("equity_minimum_percent") is not None:
        return False
    for key in ("culture", "industry", "funding_stage"):
        if pref_doc.get(key):
            return False
    wl = pref_doc.get("work_location", {})
    if wl.get("modes") or wl.get("countries"):
        return False
    geo = pref_doc.get("geography", {})
    if geo.get("regions") or geo.get("remote_friendly") is not None:
        return False
    vest = pref_doc.get("vesting", {})
    if (
        vest.get("max_cliff_months") is not None
        or vest.get("max_vesting_months") is not None
        or vest.get("prefer_accelerated") is not None
    ):
        return False
    excl = pref_doc.get("exclusions", {})
    for key in ("companies", "titles", "industries", "locations"):
        if excl.get(key):
            return False
    if pref_doc.get("priorities"):
        return False
    if pref_doc.get("notes"):
        return False
    return True


# Stale threshold: if the most recent crawl was more than this many hours ago,
# the inventory is considered stale.
STALE_HOURS = 72


@dataclass(frozen=True)
class EmptyState:
    """Canonical state label, user-facing copy, and recovery actions."""

    state: str
    title: str
    message: str
    actions: List[str] = field(default_factory=list)
    staff_detail: str = ""
    staff_only: bool = False

    def to_dict(self, *, include_staff: bool = False) -> dict:
        """Serialize to JSON-safe dict.  Staff fields are stripped unless ``include_staff``."""
        payload = {
            "state": self.state,
            "title": self.title,
            "message": self.message,
            "actions": list(self.actions),
        }
        if include_staff and self.staff_detail:
            payload["staff_detail"] = self.staff_detail
        return payload


# ---------------------------------------------------------------------------
# State labels
# ---------------------------------------------------------------------------

NO_SOURCE = "no_source"
SOURCE_DISABLED = "source_disabled"
CRAWL_RUNNING = "crawl_running"
CRAWL_FAILED = "crawl_failed"
CRAWL_STALE = "crawl_stale"
CRAWL_EMPTY = "crawl_empty"
NO_PREFERENCES = "no_preferences"
NO_MATCHES = "no_matches"
OK = "ok"


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def derive_state(
    *,
    user,
    match_count: Optional[int] = None,
    now=None,
) -> EmptyState:
    """Derive the canonical empty-state for *user*.

    The function performs cheap, read-only queries against existing signals
    (job sources, listings, crawl runs, user preferences, matches).  It never
    invents crawl state; it only reads what is already persisted.
    """
    now = now or timezone.now()

    # --- inventory-level signals -----------------------------------------

    sources_qs = JobSourceCatalog.objects.all()
    has_any_source = sources_qs.exists()
    has_enabled_source = sources_qs.filter(enabled=True).exists()

    if not has_any_source:
        return EmptyState(
            state=NO_SOURCE,
            title="No job sources configured",
            message=(
                "CRank hasn't been connected to any job sources yet. "
                "You can suggest a company for evaluation, or check back later."
            ),
            actions=["suggest_company", "help"],
            staff_detail="No JobSourceCatalog rows exist in the database.",
            staff_only=True,
        )

    if not has_enabled_source:
        return EmptyState(
            state=SOURCE_DISABLED,
            title="Job sources are being set up",
            message=(
                "Job sources exist but none are enabled yet. "
                "Check back later or suggest a company for evaluation."
            ),
            actions=["suggest_company", "help"],
            staff_detail=(
                "JobSourceCatalog rows exist but none have enabled=True."
            ),
            staff_only=True,
        )

    # Check for running crawl runs (staff-only detail)
    from crank.models.crawl_run import CrawlRun

    running_crawl = (
        CrawlRun.objects.filter(
            source_type=CrawlRun.SourceType.JOB,
            outcome=CrawlRun.Outcome.RUNNING,
        )
        .order_by("-started_at")
        .first()
    )
    if running_crawl:
        return EmptyState(
            state=CRAWL_RUNNING,
            title="Jobs are being gathered",
            message=(
                "A crawl is in progress right now. New listings should appear "
                "soon—check back shortly."
            ),
            actions=["retry"],
            staff_detail=(
                "CrawlRun {} for source_key={} started at {}.".format(
                    running_crawl.pk,
                    running_crawl.source_key,
                    running_crawl.started_at,
                )
            ),
            staff_only=True,
        )

    # Check for recently failed crawl
    failed_crawl = (
        CrawlRun.objects.filter(
            source_type=CrawlRun.SourceType.JOB,
            outcome__in=[CrawlRun.Outcome.FAILURE, CrawlRun.Outcome.TIMEOUT],
        )
        .order_by("-finished_at")
        .first()
    )
    if failed_crawl and failed_crawl.finished_at and now - failed_crawl.finished_at < timedelta(hours=24):
        return EmptyState(
            state=CRAWL_FAILED,
            title="Latest job crawl encountered a problem",
            message=(
                "The most recent crawl didn't complete successfully. "
                "The team has been notified—please check back later."
            ),
            actions=["retry", "suggest_company", "help"],
            staff_detail=(
                "CrawlRun {} outcome={} finished_at={}.".format(
                    failed_crawl.pk,
                    failed_crawl.outcome,
                    failed_crawl.finished_at,
                )
            ),
            staff_only=True,
        )

    # Check for active listings
    active_listings_count = JobListing.objects.filter(status=JobListing.Status.ACTIVE).count()

    if active_listings_count == 0:
        # Check if there were ever listings (all expired/closed)
        ever_had_listings = JobListing.all_objects.exists()
        if ever_had_listings:
            return EmptyState(
                state=CRAWL_STALE,
                title="Job listings are stale",
                message=(
                    "All previous job listings have expired or been closed. "
                    "A fresh crawl should restore listings soon."
                ),
                actions=["retry", "suggest_company"],
                staff_detail="JobListing rows exist but none have status=active.",
                staff_only=True,
            )
        else:
            return EmptyState(
                state=CRAWL_EMPTY,
                title="No job listings yet",
                message=(
                    "Job sources are enabled, but no listings have been crawled yet. "
                    "Check back later or suggest a company for evaluation."
                ),
                actions=["retry", "suggest_company", "help"],
                staff_detail=(
                    "JobSourceCatalog has enabled sources but JobListing is empty."
                ),
                staff_only=True,
            )

    # --- user-level signals ----------------------------------------------

    # Check preferences
    try:
        pref = UserPreference.objects.get(user=user)
        has_preferences = not _preferences_are_default(pref.preferences)
    except UserPreference.DoesNotExist:
        has_preferences = False

    if not has_preferences:
        return EmptyState(
            state=NO_PREFERENCES,
            title="Tell us what you're looking for",
            message=(
                "There are active job listings, but you haven't shared your "
                "preferences yet. Chat with the assistant above to set your "
                "criteria—compensation, location, culture, and more."
            ),
            actions=["chat", "help"],
        )

    # Check matches
    if match_count is None:
        match_count = JobMatch.objects.filter(
            user=user,
            dismissed=False,
            listing__status=JobListing.Status.ACTIVE,
        ).count()

    if match_count == 0:
        return EmptyState(
            state=NO_MATCHES,
            title="No matches right now",
            message=(
                "Your preferences are set and jobs are available, but none "
                "matched your criteria. Try broadening your preferences in "
                "the chat—consider wider location, different compensation, "
                "or fewer exclusions."
            ),
            actions=["chat", "suggest_company", "help"],
        )

    return EmptyState(
        state=OK,
        title="Matches ready",
        message="You have job matches ready to review.",
        actions=[],
    )


__all__ = [
    "EmptyState",
    "derive_state",
    "NO_SOURCE",
    "SOURCE_DISABLED",
    "CRAWL_RUNNING",
    "CRAWL_FAILED",
    "CRAWL_STALE",
    "CRAWL_EMPTY",
    "NO_PREFERENCES",
    "NO_MATCHES",
    "OK",
]
