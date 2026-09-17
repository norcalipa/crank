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


def _source_catalog_health(*, now, enabled_sources, freshness_window):
    """Count how many *enabled_sources* currently fail to deliver listings.

    A source is degraded when its latest job crawl failed/timed out or when it
    is stale (``last_crawl_at`` missing or older than the freshness window),
    mirroring the signals in :mod:`crank.services.inventory_health`.
    Returns the number of degraded sources.
    """
    from crank.models.crawl_run import CrawlRun
    from crank.services.inventory_health import FAILURE_OUTCOMES

    degraded = 0
    for source in enabled_sources:
        latest = (
            CrawlRun.objects.filter(
                job_source=source,
                source_type=CrawlRun.SourceType.JOB,
            )
            .order_by("-started_at", "-id")
            .first()
        )
        if latest is not None and latest.outcome in FAILURE_OUTCOMES:
            degraded += 1
            continue
        last_crawl_at = getattr(source, "last_crawl_at", None)
        if last_crawl_at is None or (
            freshness_window is not None and now - last_crawl_at >= freshness_window
        ):
            degraded += 1
    return degraded


def _coverage(*, now, freshness_window) -> dict | None:
    """Bounded coverage facts over enabled sources, or None when not partial.

    Partial coverage means at least one enabled source is delivering and at
    least one is not (failing or stale), so results exist but may be
    incomplete.
    """
    enabled_sources = list(
        JobSourceCatalog.objects.filter(enabled=True).order_by("pk")
    )
    enabled_count = len(enabled_sources)
    if enabled_count == 0:
        return None
    degraded = _source_catalog_health(
        now=now, enabled_sources=enabled_sources, freshness_window=freshness_window
    )
    if not (0 < degraded < enabled_count):
        return None
    return {"enabled_sources": enabled_count, "failing_sources": degraded}


def _inventory_facts(*, now, active_listings_count) -> dict:
    """Bounded, user-safe inventory facts for zero-match explanations."""
    from crank.models.crawl_run import CrawlRun

    last_success = (
        CrawlRun.objects.filter(
            source_type=CrawlRun.SourceType.JOB,
            outcome__in=[CrawlRun.Outcome.SUCCESS, CrawlRun.Outcome.PARTIAL],
            finished_at__isnull=False,
        )
        .order_by("-finished_at", "-id")
        .values_list("finished_at", flat=True)
        .first()
    )
    age_hours = None
    if last_success is not None:
        age_hours = round((now - last_success).total_seconds() / 3600.0, 1)
    return {
        "active_listings": active_listings_count,
        "last_success_at": (
            last_success.isoformat() if last_success is not None else None
        ),
        "age_hours": age_hours,
    }


def _bounded_join(values, limit: int = 4) -> str:
    shown = [str(v) for v in list(values)[:limit]]
    text = ", ".join(shown)
    if len(values) > limit:
        text += f" and {len(values) - limit} more"
    return text


def _active_constraints(pref_doc: dict) -> list[str]:
    """Human-readable hard constraints derived from the preference document.

    Walks the same sections :func:`_preferences_are_default` walks. Values are
    the user's own saved requirements, bounded in count and length.
    """
    constraints: list[str] = []
    if not isinstance(pref_doc, dict):
        return constraints
    comp = pref_doc.get("compensation") or {}
    minimum_salary = comp.get("minimum_salary")
    if minimum_salary is not None:
        try:
            constraints.append(f"Minimum salary {int(minimum_salary):,}")
        except (TypeError, ValueError):
            pass
    work_location = pref_doc.get("work_location") or {}
    modes = work_location.get("modes") or []
    if modes:
        constraints.append(f"Work location: {_bounded_join(modes)}")
    countries = work_location.get("countries") or []
    if countries:
        constraints.append(f"Countries: {_bounded_join(countries)}")
    geography = pref_doc.get("geography") or {}
    regions = geography.get("regions") or []
    if regions:
        constraints.append(f"Regions: {_bounded_join(regions)}")
    for key, label in (("industry", "Industries"), ("funding_stage", "Funding stages"), ("culture", "Culture")):
        values = pref_doc.get(key) or []
        if values:
            constraints.append(f"{label}: {_bounded_join(values)}")
    exclusions = pref_doc.get("exclusions") or {}
    for key, label in (
        ("companies", "Excluded companies"),
        ("titles", "Excluded titles"),
        ("industries", "Excluded industries"),
        ("locations", "Excluded locations"),
    ):
        values = exclusions.get(key) or []
        if values:
            constraints.append(f"{label}: {_bounded_join(values)}")
    vesting = pref_doc.get("vesting") or {}
    if vesting.get("max_cliff_months") is not None:
        constraints.append(f"Vesting cliff at most {vesting['max_cliff_months']} months")
    if vesting.get("max_vesting_months") is not None:
        constraints.append(f"Vesting at most {vesting['max_vesting_months']} months")
    return constraints[:8]


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
    """Canonical state label, user-facing copy, and recovery actions.

    The newer fields are additive contract extensions: ``refreshing`` marks a
    refresh-in-progress that must be rendered *alongside* results (never
    instead of them), ``coverage`` explains degraded multi-source coverage,
    ``active_constraints`` lists the user's hard requirements on a genuine
    zero-match, and ``inventory`` carries bounded inventory facts.
    """

    state: str
    title: str
    message: str
    actions: List[str] = field(default_factory=list)
    staff_detail: str = ""
    staff_only: bool = False
    refreshing: bool = False
    coverage: dict | None = None
    active_constraints: list[str] = field(default_factory=list)
    inventory: dict | None = None

    def to_dict(self, *, include_staff: bool = False) -> dict:
        """Serialize to JSON-safe dict.  Staff fields are stripped unless ``include_staff``.

        New fields are emitted only when set so existing clients that tolerate
        absent keys keep working (additive-only contract).
        """
        payload = {
            "state": self.state,
            "title": self.title,
            "message": self.message,
            "actions": list(self.actions),
        }
        if include_staff and self.staff_detail:
            payload["staff_detail"] = self.staff_detail
        if self.refreshing:
            payload["refreshing"] = True
        if self.coverage is not None:
            payload["coverage"] = dict(self.coverage)
        if self.active_constraints:
            payload["active_constraints"] = list(self.active_constraints)
        if self.inventory is not None:
            payload["inventory"] = dict(self.inventory)
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
PARTIAL_COVERAGE = "partial_coverage"
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

    Precedence: inventory-level checks win for zero-inventory cases, but
    user-level results beat freshness signals — a refresh-in-progress or a
    recently failed crawl becomes a notice carried on top of results (the
    ``refreshing`` flag / ``partial_coverage`` state), never a replacement
    for them.  A running crawl still yields ``crawl_running`` only when there
    is nothing to show yet (first gather).
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
                "CRank hasn't been connected to any job sources yet, so job "
                "openings can't be confirmed right now. You can explore the "
                "company rankings in the meantime, or suggest a company for "
                "evaluation."
            ),
            actions=["explore_companies", "suggest_company", "help"],
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

    # Check for recently failed crawl
    failed_crawl = (
        CrawlRun.objects.filter(
            source_type=CrawlRun.SourceType.JOB,
            outcome__in=[CrawlRun.Outcome.FAILURE, CrawlRun.Outcome.TIMEOUT],
        )
        .order_by("-finished_at")
        .first()
    )
    recent_failed_crawl = (
        failed_crawl
        if failed_crawl
        and failed_crawl.finished_at
        and now - failed_crawl.finished_at < timedelta(hours=24)
        else None
    )

    # Check for active listings
    active_listings_count = JobListing.objects.filter(status=JobListing.Status.ACTIVE).count()

    if active_listings_count == 0:
        # Zero-inventory cases keep inventory-level precedence.  A running
        # crawl or recent failure is the whole story only when there is
        # nothing else to show yet.
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
        if recent_failed_crawl:
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
                        recent_failed_crawl.pk,
                        recent_failed_crawl.outcome,
                        recent_failed_crawl.finished_at,
                    )
                ),
                staff_only=True,
            )
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
                actions=["retry", "suggest_company", "explore_companies"],
                staff_detail="JobListing rows exist but none have status=active.",
                staff_only=True,
            )
        return EmptyState(
            state=CRAWL_EMPTY,
            title="No job listings yet",
            message=(
                "Job sources are enabled, but no listings have been crawled yet. "
                "Check back later or suggest a company for evaluation."
            ),
            actions=["retry", "suggest_company", "help", "explore_companies"],
            staff_detail=(
                "JobSourceCatalog has enabled sources but JobListing is empty."
            ),
            staff_only=True,
        )

    # --- freshness becomes a notice, never a replacement for results ------

    refreshing = running_crawl is not None
    from crank.services.inventory_health import freshness_hours

    freshness_window = None
    hours = freshness_hours()
    if hours > 0:
        freshness_window = timedelta(hours=hours)
    coverage = _coverage(now=now, freshness_window=freshness_window)

    # --- user-level signals ----------------------------------------------

    # Check preferences
    pref = None
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

    if match_count > 0:
        if coverage is not None:
            return EmptyState(
                state=PARTIAL_COVERAGE,
                title="Coverage is limited right now",
                message=(
                    "Your matches come from the job sources that are working, "
                    "but not every source is returning listings, so some "
                    "openings may be missing. Results refresh automatically."
                ),
                actions=["retry", "explore_companies", "suggest_company", "help"],
                staff_detail=(
                    f"Coverage: {coverage['failing_sources']} of "
                    f"{coverage['enabled_sources']} enabled source(s) degraded "
                    "(failed, timed out, or stale)."
                ),
                staff_only=True,
                refreshing=refreshing,
                coverage=coverage,
                inventory=_inventory_facts(
                    now=now, active_listings_count=active_listings_count
                ),
            )
        return EmptyState(
            state=OK,
            title="Matches ready",
            message="You have job matches ready to review.",
            actions=[],
            refreshing=refreshing,
        )

    # Genuine evaluated zero-match: surface the user's own hard constraints
    # and bounded inventory facts so "no matches" is never mistaken for
    # "nothing available yet".
    pref_doc = pref.preferences if pref is not None else {}
    return EmptyState(
        state=NO_MATCHES,
        title="No matches for your current requirements",
        message=(
            "Jobs are available, but none meet your saved requirements yet. "
            "Your active requirements are listed below—chat with the "
            "assistant to adjust them, or explore companies while you decide."
        ),
        actions=["chat", "explore_companies", "suggest_company", "help"],
        refreshing=refreshing,
        active_constraints=_active_constraints(pref_doc),
        inventory=_inventory_facts(
            now=now, active_listings_count=active_listings_count
        ),
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
    "PARTIAL_COVERAGE",
    "OK",
]
