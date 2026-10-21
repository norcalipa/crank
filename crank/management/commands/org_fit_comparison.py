# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Read-only before-and-after comparison of the ranked company list (issue #584b).

For ten synthetic reader profiles defined below, over the active public
organizations in the current database, it prints the order the ranked list had
before ("old": the removed factor-weighted score) and the order it has now
("new": confirmed company-level priorities, "n of m", then the ratings
average, then the organization id). Both orders are ranked over every company
that survives the hard filter, so a company that leaves the 25-row list counts
as a fall. It prints how many of the old top 10 stay in the new top 10, the
largest rise and fall in rank, and how many companies enter or leave the list.

It describes the assistant's list (25 rows from the first 100 candidates). The
job-match panel reads fewer rows from a smaller candidate window, so its list
can differ when more than 40 companies are active and public.

The profiles are synthetic documents held in this module. No user's
preferences are read, no row is written and no telemetry is recorded (the
shared ratings-average cache is filled as a side effect of reading ratings). The
"old" arithmetic is a frozen copy of the scorer this change removed, kept here
only so the two orders can be put side by side.
"""
from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from crank.agents.jobs.matching import JobCriteria, _canonical_stage, project_criteria
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG
from crank.models.organization import Organization
from crank.models.preference import SCHEMA_VERSION, default_preferences
from crank.services import job_matching

TOP = 10


def _profile(**sections: Any) -> dict[str, Any]:
    document = default_preferences()
    for name, values in sections.items():
        if isinstance(values, dict) and isinstance(document.get(name), dict):
            document[name].update(values)
        else:
            document[name] = values
    return document


def synthetic_profiles(excluded_company: str) -> list[tuple[str, dict[str, Any]]]:
    """The ten reader profiles the comparison runs, as preference documents."""
    return [
        ("remote-only", _profile(work_location={"modes": ["remote"]})),
        ("hybrid-3-days-or-less", _profile(work_location={"modes": ["hybrid"], "max_in_office_days": 3})),
        ("public-company-required", _profile(compensation={"require_public_company": True})),
        ("series-b-to-d", _profile(funding_stage=["B", "C", "D"])),
        ("accelerated-vesting", _profile(vesting={"prefer_accelerated": True})),
        ("us-only", _profile(work_location={"countries": ["us"]})),
        (
            "remote-and-series-b-to-d",
            _profile(work_location={"modes": ["remote"]}, funding_stage=["B", "C", "D"]),
        ),
        (
            "hybrid-us-accelerated",
            _profile(
                work_location={"modes": ["hybrid"], "countries": ["us"]},
                vesting={"prefer_accelerated": True},
            ),
        ),
        (
            "per-job-priorities-only",
            _profile(compensation={"minimum_salary": 150000, "currency": "USD"}, industry=["software"]),
        ),
        (
            "public-company-with-an-exclusion",
            _profile(
                compensation={"require_public_company": True},
                exclusions={"companies": [excluded_company] if excluded_company else []},
            ),
        ),
    ]


def old_score(organization: Any, criteria: JobCriteria) -> float:
    """The removed factor-weighted score, frozen: profile columns and the ratings average."""
    config = DEFAULT_CONFIG
    max_score = float(config.max_score)
    score = 0.0
    funding = getattr(organization, "funding_round", None)
    if funding and criteria.funding_stages:
        wanted = frozenset(_canonical_stage(s) for s in criteria.funding_stages)
        if _canonical_stage(funding) in wanted:
            score += max_score * float(config.weights.get("funding_stage", 0.0))
    mode = {"R": "remote", "H": "hybrid", "O": "in-office"}.get(getattr(organization, "rto_policy", None), "")
    if mode and mode in criteria.work_modes:
        score += max_score * float(config.weights.get("work_location", 0.0))
    rating = job_matching._company_score(organization)
    if rating is not None:
        score += max_score * float(config.weights.get("organization_scores", 0.0)) * (rating / 5.0)
    accelerated = getattr(organization, "accelerated_vesting", None)
    if criteria.prefer_accelerated is not None and isinstance(accelerated, bool):
        weight = float(config.weights.get("vesting", 0.0))
        score += max_score * weight * (1.0 if accelerated == criteria.prefer_accelerated else 0.0)
    return round(min(max_score, score), 10)


def _figure(met: Any) -> str:
    return f"{met['met']} of {met['counted']}" if met else "-"


class Command(BaseCommand):
    help = "Compare the ranked company list's old and new order for ten synthetic readers (read-only)."

    def handle(self, *args, **options):
        out = self.stdout.write
        cap = job_matching.MAX_MATCH_RESULTS
        # The assistant's list: the first 100 candidates (cap * 4), as match_organizations reads them;
        # the panel's smaller window is not run here.
        organizations = list(Organization.objects.filter(status=1, public=True)[: cap * 4])
        evidence = job_matching._resolve_evidence([int(o.pk) for o in organizations])
        out("org_fit_comparison (read-only; synthetic readers; no user data read; no rows written; ratings cache filled)")
        out(f"organizations={len(organizations)}")
        first = organizations[0].name if organizations else ""
        for name, document in synthetic_profiles(first):
            criteria = project_criteria(document, SCHEMA_VERSION)
            survivors = [
                o for o in organizations
                if not job_matching._org_excluded(o, criteria, evidence.get(int(o.pk)))
            ]
            old = sorted(((old_score(o, criteria), int(o.pk), o.name) for o in survivors), key=lambda t: (-t[0], t[1]))
            met = {
                int(o.pk): job_matching.priorities_met(
                    job_matching.evaluate_org_requirements(o, criteria, evidence.get(int(o.pk)))
                )
                for o in survivors
            }
            # The list's own key, over every survivor (not only the 25 rows it shows).
            new = sorted(
                (job_matching.org_sort_key(met[int(o.pk)], job_matching._company_score(o), int(o.pk)), o.name)
                for o in survivors
            )
            new = [(key[2], name) for key, name in new]
            old_rank = {pk: i for i, (_, pk, _name) in enumerate(old)}
            new_rank = {pk: i for i, (pk, _name) in enumerate(new)}
            kept = len({pk for _, pk, _n in old[:TOP]} & {pk for pk, _n in new[:TOP]})
            left = len({pk for _, pk, _n in old[:cap]} - {pk for pk, _n in new[:cap]})
            moves = [(old_rank[pk] - new_rank[pk], pk) for pk in new_rank]
            names = {int(o.pk): o.name for o in organizations}
            rise = max(moves, default=(0, None))
            fall = min(moves, default=(0, None))
            out("")
            out(f"profile={name} survivors={len(survivors)}")
            out("  old top 10:")
            for i, (score, _pk, org_name) in enumerate(old[:TOP], 1):
                out(f"    {i:>2}. {org_name} score={score:.1f}")
            out("  new top 10:")
            for i, (pk, org_name) in enumerate(new[:TOP], 1):
                out(f"    {i:>2}. {org_name} priorities_met={_figure(met[pk])}")
            out(f"  old_top10_still_in_new_top10={kept} of {min(TOP, len(old))}")
            out(
                f"  largest_rise={max(rise[0], 0)}"
                + (f" ({names[rise[1]]})" if rise[0] > 0 else "")
                + f" largest_fall={max(-fall[0], 0)}"
                + (f" ({names[fall[1]]})" if fall[0] < 0 else "")
            )
            out(f"  left_the_{cap}_row_list={left} (they rank below {cap} under the new order)")
