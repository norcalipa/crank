# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Persist deterministic job-ranking results for an owner."""

from django.db import transaction
from django.utils import timezone

from crank.agents.jobs.matching import rank_listings
from crank.models.job import JobListing
from crank.models.job_match import JobMatch
from crank.models.preference import UserPreference
from crank.services.company_evidence import resolve_field_evidence_for_orgs
from crank.services.publication import listing_data_revisions


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


def _org_id(listing):
    organization = getattr(listing, "organization", None)
    if organization is not None and getattr(organization, "pk", None):
        return int(organization.pk)
    return None


def _preference_revision(user):
    """The user's current document revision, or ``None`` when absent."""
    try:
        return UserPreference.objects.get(user=user).revision
    except UserPreference.DoesNotExist:
        return None


def persist_matches(user, listings, criteria, config):
    """Rank and persist non-excluded, active listings for ``user``.

    The unique key includes the owner, listing, preference version, and ranker
    version. Re-running a ranking pass therefore updates the existing result's
    score, explanation, and freshness instead of creating another row.
    Dismissed rows remain dismissed when refreshed. Result-revision columns
    (issue #467) are additive, non-key fields written on both branches.
    """
    active_status = JobListing.Status.ACTIVE
    listings = tuple(listings)
    active = [listing for listing in listings if listing.status == active_status]
    org_ids = {_org_id(listing) for listing in active if _org_id(listing) is not None}
    evidence = resolve_field_evidence_for_orgs(org_ids)
    data_revisions = listing_data_revisions(active)
    preference_revision = _preference_revision(user)

    ranked = rank_listings(active, criteria, config, evidence=evidence)
    listing_by_id = {
        listing.pk: listing
        for listing in active
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
            org_id = _org_id(listing)
            org_evidence = evidence.get(org_id) or {} if org_id is not None else {}
            evidence_ids = sorted(
                {ev.pk for ev in org_evidence.values() if getattr(ev, "pk", None) is not None}
            )
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
                "preference_revision": preference_revision,
                "data_revision": data_revisions.get(listing.pk),
                "generated_at": now,
                "requirements": [outcome.as_dict() for outcome in result.requirements],
                "evidence_ids": evidence_ids,
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
                match.preference_revision = preference_revision
                match.data_revision = data_revisions.get(listing.pk)
                match.generated_at = now
                match.requirements = defaults["requirements"]
                match.evidence_ids = evidence_ids
                match.last_matched_at = now
                match.save(
                    update_fields=[
                        "organization",
                        "score",
                        "factors",
                        "preference_revision",
                        "data_revision",
                        "generated_at",
                        "requirements",
                        "evidence_ids",
                        "last_matched_at",
                        "modified",
                    ]
                )
            count += 1
    return count


__all__ = ["persist_matches"]