# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Persist deterministic job-ranking results for an owner."""

from django.db import transaction
from django.utils import timezone

from crank.agents.jobs.matching import rank_listings
from crank.models.job import JobListing
from crank.models.job_match import JobMatch


def _factor_data(factors):
    """Convert ranking factor dataclasses into JSON-safe dictionaries."""
    return [
        {
            "factor": factor.factor,
            "score": factor.score,
            "max_score": factor.max_score,
            "detail": factor.detail,
        }
        for factor in factors
    ]


def persist_matches(user, listings, criteria, config):
    """Rank and persist non-excluded, active listings for ``user``.

    The unique key includes the owner, listing, preference version, and ranker
    version. Re-running a ranking pass therefore updates the existing result's
    score, explanation, and freshness instead of creating another row.
    Dismissed rows remain dismissed when refreshed.
    """
    active_status = JobListing.Status.ACTIVE
    listings = tuple(listings)
    ranked = rank_listings(
        (listing for listing in listings if listing.status == active_status),
        criteria,
        config,
    )
    listing_by_id = {
        listing.pk: listing
        for listing in listings
        if listing.status == active_status
    }
    now = timezone.now()
    count = 0
    with transaction.atomic():
        for result in ranked:
            if result.excluded:
                continue
            listing = listing_by_id.get(result.listing_id)
            if listing is None:
                continue
            lookup = {
                "user": user,
                "listing": listing,
                "preference_version": result.criteria_version,
                "ranker_version": result.ranker_version,
            }
            defaults = {
                "organization": listing.organization,
                "score": result.score,
                "factors": _factor_data(result.factors),
                "first_matched_at": now,
                "last_matched_at": now,
            }
            match, created = JobMatch.objects.get_or_create(
                **lookup,
                defaults=defaults,
            )
            if not created:
                match.organization = listing.organization
                match.score = result.score
                match.factors = defaults["factors"]
                match.last_matched_at = now
                match.save(
                    update_fields=[
                        "organization",
                        "score",
                        "factors",
                        "last_matched_at",
                        "modified",
                    ]
                )
            count += 1
    return count


__all__ = ["persist_matches"]
