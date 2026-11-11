# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Read-only report on how office-policy prose is read (issue #537).

For every distinct accepted ``rto_policy`` evidence value it prints how many
organizations carry it, the in-office days the reader read before ranker 1.1.0
(``legacy_rto_days``, a frozen copy: "today" in the corpus) and the days the
current reader (``matching._rto_days``, "proposed") reads. It then prints
aggregate counts of readers whose hard or soft work-location requirements see a
different set of companies excluded, or a different number of companies
matched, between the two.

Nothing is written and no telemetry is recorded. The output holds the evidence
prose itself and counts; it never holds a user identifier or a preference value.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from typing import Any

from django.core.management.base import BaseCommand

from crank.agents.jobs import matching
from crank.agents.jobs.matching import MATCH, MISMATCH, UNKNOWN
from crank.models.company_profile import CompanyFieldEvidence
from crank.models.preference import UserPreference

_WORK_PATHS = ("work_location.modes", "work_location.max_in_office_days")

def legacy_rto_days(value: Any) -> int | None:
    """Frozen copy of the reader before ranker 1.1.0: the "today" column of the corpus."""
    normalized = matching._normalized(value).replace("_", " ")
    mapping = {
        "r": 0, "remote": 0,
        "h": 3, "hybrid": 3,
        "o": 5, "in-office": 5, "in office": 5, "onsite": 5,
    }
    if normalized in mapping:
        return mapping[normalized]
    if any(token in normalized for token in ("in-office", "in office", "onsite", "office")):
        return 5
    if "hybrid" in normalized:
        return 3
    if "remote" in normalized:
        return 0
    return None


def _legacy_mode(days: int | None) -> str | None:
    return {0: "remote", 3: "hybrid", 5: "in-office"}.get(days)


def _outcome(path: str, days: int | None, mode_of: Callable[[int | None], str | None], criteria) -> str:
    if path == "work_location.modes":
        mode = mode_of(days)
        if mode is None:
            return UNKNOWN
        return MATCH if mode in criteria.work_modes else MISMATCH
    if days is None:
        return UNKNOWN
    return MATCH if days <= criteria.max_in_office_days else MISMATCH


def _view(criteria, days, mode_of) -> tuple[bool, bool]:
    """(excluded by a hard requirement, matches every work-location requirement)."""
    excluded = False
    matched = True
    for path in matching._requirement_set_paths(criteria):
        if path not in _WORK_PATHS:
            continue
        status = _outcome(path, days, mode_of, criteria)
        matched = matched and status == MATCH
        excluded = excluded or (status != MATCH and matching._is_hard(criteria, path))
    return excluded, matched


class Command(BaseCommand):
    help = "Report how accepted office-policy evidence is read today and under the proposed reader (read-only)."

    def handle(self, *args, **options):
        rows = CompanyFieldEvidence.objects.filter(
            field_key=CompanyFieldEvidence.FieldKey.RTO_POLICY,
            state=CompanyFieldEvidence.State.ACCEPTED,
        ).values_list("organization_id", "value_text")
        orgs_by_value: dict[str, set[int]] = {}
        for organization_id, value in rows:
            orgs_by_value.setdefault(value, set()).add(organization_id)
        values = sorted(orgs_by_value)
        readings = {v: (legacy_rto_days(v), matching._rto_days(v)) for v in values}

        out = self.stdout.write
        out("rto_reading_report (read-only; nothing written)")
        out(f"distinct_values={len(values)} organizations={len({o for s in orgs_by_value.values() for o in s})}")
        changed = 0
        for value in values:
            today, proposed = readings[value]
            same = today == proposed
            changed += 0 if same else 1
            out(
                f"value={json.dumps(value)} organizations={len(orgs_by_value[value])} "
                f"today={today} proposed={proposed} {'same' if same else 'CHANGED'}"
            )
        out(f"values_changed={changed}")
        unknown_values = [sum(1 for v in values if readings[v][i] is None) for i in (0, 1)]
        unknown_orgs = [
            len({o for v in values if readings[v][i] is None for o in orgs_by_value[v]}) for i in (0, 1)
        ]
        out(
            f"values_unknown_today={unknown_values[0]} values_unknown_proposed={unknown_values[1]} "
            f"organizations_unknown_today={unknown_orgs[0]} organizations_unknown_proposed={unknown_orgs[1]}"
        )

        tally: Counter = Counter()
        for preferences, schema_version in UserPreference.objects.values_list("preferences", "schema_version"):
            criteria = matching.project_criteria(preferences, schema_version)
            if not any(p in _WORK_PATHS for p in matching._requirement_set_paths(criteria)):
                continue
            tally["readers_with_work_location_requirement"] += 1
            excluded_differs = False
            matched_today = matched_proposed = 0
            for value, orgs in orgs_by_value.items():
                today, proposed = readings[value]
                ex_t, ma_t = _view(criteria, today, _legacy_mode)
                ex_p, ma_p = _view(criteria, proposed, matching._mode_for_days)
                excluded_differs = excluded_differs or ex_t != ex_p
                matched_today += len(orgs) * ma_t
                matched_proposed += len(orgs) * ma_p
            tally["readers_with_different_excluded_set"] += excluded_differs
            tally["readers_with_different_matched_count"] += matched_today != matched_proposed
        for key in (
            "readers_with_work_location_requirement",
            "readers_with_different_excluded_set",
            "readers_with_different_matched_count",
        ):
            out(f"{key}={tally[key]}")
