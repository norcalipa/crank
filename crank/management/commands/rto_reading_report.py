# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Read-only report on how office-policy prose is read (issue #537, stage 1).

For every distinct accepted ``rto_policy`` evidence value it prints how many
organizations carry it, the in-office days matching reads today
(``matching._rto_days``) and the days the proposed reader would read. It then
prints aggregate counts of readers whose hard or soft work-location
requirements would see a different set of companies excluded, or a different
number of companies matched.

Nothing is written and no telemetry is recorded. The output holds the evidence
prose itself and counts; it never holds a user identifier or a preference value.

``proposed_rto_days`` is a frozen copy of the reader the corpus
(``crank/tests/agents/rto_policy_corpus.py``) proposes. Stage 1 changes no
behaviour: the production reader is untouched. Stage 2 replaces
``matching._rto_days`` with this reader and this module then reports against
the real one.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from django.core.management.base import BaseCommand

from crank.agents.jobs import matching
from crank.agents.jobs.matching import MATCH, MISMATCH, UNKNOWN
from crank.models.company_profile import CompanyFieldEvidence
from crank.models.preference import UserPreference

_WORK_PATHS = ("work_location.modes", "work_location.max_in_office_days")

_EXACT = {
    "r": 0, "remote": 0,
    "h": 3, "hybrid": 3,
    "o": 5, "in-office": 5, "in office": 5, "onsite": 5, "on-site": 5, "on site": 5,
}

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_NUMBER = r"(?:one|two|three|four|five|[1-5])"
_OFFICE_WORD = re.compile(r"office|on-?site|on site|in-?person|\brto\b|hybrid|return")
_RANGE = re.compile(rf"\b{_NUMBER}\s*(?:-|to|or)\s*{_NUMBER}\s*(?:days?|x)\b")
_COUNT = re.compile(rf"\b({_NUMBER})[ -]?(?:days?|x)\b")
_REMOTE_DAYS = re.compile(
    r"\s*(?:(?:a|per|each) week |/?wk |week )?(?:remote(?! (?:mon|tue|wed|thu|fri|not|never|by|on|only|is|allowed|permitted|except))|from home|at home|wfh)"
)
_FULL_WEEK = re.compile(
    r"\bmon(?:day)?\s*(?:-|to|through|thru)?\s*fri(?:day)?\b|\bfull[ -]time (?:in|at|on)\b|\bevery day\b|\ball week\b"
)
_MIXED = re.compile(
    r"\bflexib|\bdepends\b|\bvaries\b|\bvary\b|\bcase by case\b|\bby team\b|\bper team\b|\bup to the team\b|"
    r"\b(?:remote|hybrid) (?:or|and) (?:remote|hybrid)\b|\bhybrid remote\b|\bremote hybrid\b|"
    r"\b(?:remote|hybrid) (?:or|and) (?:in[ -])?office\b|\b(?:in[ -])?office (?:or|and) (?:remote|hybrid)\b"
)
_OFFICE = r"(?:in[ -]?office|office|on-?site|on site)"
_OPTIONAL_OFFICE = re.compile(
    rf"\boptional {_OFFICE}\b|\b{_OFFICE}(?: \w+)? (?:is |are )?(?:optional|voluntary|not (?:required|needed|mandatory))\b|"
    rf"\bno {_OFFICE}(?: (?:is |are )?(?:required|needed|mandatory|requirement|attendance|mandate|expected|obligation|policy|days)|$)"
)
_OCCASIONAL = re.compile(
    r"\bquarterly\b|\boccasional|\bmonthly\b|\bannual|\boffsites?\b|\boff-sites?\b|\bonboarding\b|\bkick-?off\b|"
    r"\bretreats?\b|\bfew times a year\b|\btwice a year\b|\bonce a (?:month|quarter|year)\b"
)
_IN_OFFICE = re.compile(
    r"\boffice[ -]first\b|\bin[ -]office\b|\bon-?site\b|\bon site\b|\breturn[ -]to[ -](?:the[ -])?office\b|\brto\b|"
    r"\boffice[ -]based\b|\boffice attendance\b|\bin[ -]person\b|\bat the office\b|^(?:the )?office$"
)
_NEGATED_REMOTE = re.compile(
    r"\b(?:not|no|non)[ -]remote\b|\bremote (?:work )?(?:is |are )?(?:not|never) (?:permitted|allowed|available|offered|an option)\b|"
    r"\bremote (?:work )?unavailable\b"
)
_NEGATED_HYBRID = re.compile(r"\b(?:not|no|non)[ -]hybrid\b")
_WEEKDAYS = re.compile(r"\b(?:mon|tue|tues|wed|thu|thur|thurs|fri|monday|tuesday|wednesday|thursday|friday)\b")


def proposed_rto_days(value: Any) -> int | None:
    """The proposed reading of an office-policy value (frozen copy for stage 1).

    An ordered rule list over the normalized text; the first rule that fires
    wins. ``None`` means unknown: never a guess. See ``issue #537`` for the
    rules and ``rto_policy_corpus.py`` for the contract.
    """
    text = matching._normalized(value).replace("_", " ")
    if text in _EXACT:
        return _EXACT[text]
    # 1. Mixed or conditional wording is unknown.
    if _MIXED.search(text):
        return None
    # 2. Negated or optional office, with no attendance count, is remote.
    if _OPTIONAL_OFFICE.search(text):
        return 0
    # 3. Occasional attendance (offsites, onboarding, quarterly) is unknown.
    if _OCCASIONAL.search(text):
        return None
    has_office_word = _OFFICE_WORD.search(text) is not None
    # 4. An explicit day count next to an office word is that count.
    if has_office_word:
        if _RANGE.search(text):
            return None
        counts = list(_COUNT.finditer(text))
        if counts:
            match = counts[0]
            if _REMOTE_DAYS.match(text, match.end()):
                return None  # the count is of remote days
            word = match.group(1)
            return _NUMBER_WORDS.get(word) or int(word)
        if _FULL_WEEK.search(text):
            return 5
    # 5. Hybrid on named weekdays is unknown (matching does not count them).
    if "hybrid" in text and _WEEKDAYS.search(text):
        return None
    # 6. Office-first / return-to-office / in-office wording is five days.
    if _IN_OFFICE.search(text):
        return 5
    # 7. Negated remote says nothing about hybrid or in-office; negated hybrid
    # says nothing either unless the text goes on to say remote.
    negated_hybrid = _NEGATED_HYBRID.search(text) is not None
    if _NEGATED_REMOTE.search(text) or (negated_hybrid and "remote" not in text):
        return None
    # 8. Plain hybrid keeps today's three days.
    if "hybrid" in text and not negated_hybrid:
        return 3
    # 9. Remote wording, including an incidental office mention, is remote.
    if "remote" in text:
        return 0
    return None


def _mode(days: int | None) -> str | None:
    if days is None:
        return None
    return "remote" if days == 0 else "in-office" if days == 5 else "hybrid"


def _today_mode(days: int | None) -> str | None:
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
        readings = {v: (matching._rto_days(v), proposed_rto_days(v)) for v in values}

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
                ex_t, ma_t = _view(criteria, today, _today_mode)
                ex_p, ma_p = _view(criteria, proposed, _mode)
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
