# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The office-policy reading corpus (issue #537).

``_rto_days`` returns the owner-approved ``proposed`` column for every row. The
``today`` column stays as the record: the report command keeps a frozen copy of
the earlier reader and this module checks it still reproduces that column.
"""
import itertools

import pytest

from crank.agents.jobs.matching import _mode_for_days, _mode_from_rto, _rto_days
from crank.management.commands.rto_reading_report import legacy_rto_days
from crank.services.company_evidence import matching_reading
from crank.tests.agents.rto_policy_corpus import CORPUS

ROWS = [pytest.param(*row, id=f"{i}-{row[3]}") for i, row in enumerate(CORPUS)]


def test_corpus_is_large_and_well_formed():
    assert len(CORPUS) >= 150
    for text, today, proposed, category, source in CORPUS:
        assert isinstance(text, str)
        assert today is None or today in range(6)
        assert proposed is None or proposed in range(6)
        assert category and source
    assert len({row[3] for row in CORPUS}) >= 15
    assert any(today != proposed for _, today, proposed, _, _ in CORPUS)


def test_the_same_text_has_one_reading_wherever_it_is_listed():
    seen = {}
    for text, today, proposed, _category, _source in CORPUS:
        assert seen.setdefault(text, (today, proposed)) == (today, proposed), text


@pytest.mark.parametrize("text,today,proposed,category,source", ROWS)
def test_reader_returns_the_approved_reading(text, today, proposed, category, source):
    assert _rto_days(text) == proposed


@pytest.mark.parametrize("text,today,proposed,category,source", ROWS)
def test_the_today_column_is_the_earlier_reading(text, today, proposed, category, source):
    assert legacy_rto_days(text) == today


@pytest.mark.parametrize("text,today,proposed,category,source", ROWS)
def test_matching_reading_never_raises(text, today, proposed, category, source):
    reading = matching_reading("rto_policy", text)
    assert reading.startswith("matching ")
    assert _mode_from_rto(text) == _mode_for_days(proposed)


def test_reader_never_reads_stated_in_office_policies_as_remote():
    """The #525 failure mode, stated against what the reader returns."""
    in_office = {"office-first", "in-office-count", "in-office-weekdays", "onsite-spelling"}
    for text, _today, _proposed, category, _source in CORPUS:
        days = _rto_days(text)
        if category in in_office:
            assert days != 0, text
        if category == "negated-remote":
            assert days is None, text
        if category in {"mixed", "occasional", "empty"}:
            assert days is None, text
        if category in {"office-optional", "remote-incidental-office"}:
            assert days != 5, text
        if category == "remote-incidental-office":
            assert days == 0, text


def test_reader_mode_by_range():
    assert [_mode_for_days(d) for d in (None, 0, 1, 2, 3, 4, 5)] == [
        None, "remote", "hybrid", "hybrid", "hybrid", "hybrid", "in-office",
    ]


def test_matching_reading_labels_by_range():
    expected = {0: "remote", 1: "hybrid", 2: "hybrid", 3: "hybrid", 4: "hybrid", 5: "in-office"}
    for days, label in expected.items():
        text = {0: "Remote", 5: "In-Office"}.get(days, f"Hybrid, {days} days in office")
        unit = "in-office day" if days == 1 else "in-office days"
        assert matching_reading("rto_policy", text) == f"matching reads this as: {label} ({days} {unit})"
    assert matching_reading("rto_policy", "It depends") == "matching cannot read this (unknown)"


# Generated properties (plan criterion 6 beyond the corpus's own strings): the
# reader is checked against templates the corpus does not list.
IN_OFFICE_TEMPLATES = [
    "Office required",
    "In the office",
    "Work from our Austin office",
    "Employees work from the office",
    "Onsite at HQ",
    "In-person company",
]
IN_OFFICE_COUNT_TEMPLATES = [
    ("In-office 4 days a week", 4),
    ("3 days in office", 3),
    ("Onsite 2 days per week", 2),
    ("Return to office 4 days a week", 4),
    ("In-person five days", 5),
]
REMOTE_EXCEPTION_CLAUSES = [
    "; remote by exception",
    ", remote only with VP approval",
    "; we don't offer remote positions",
    ". Remote work is not an option.",
    " (remote for emergencies)",
    "; remote exceptions are rare",
]
OPTIONAL_OFFICE_CLAUSES = [
    " (office optional on Fridays)",
    "; office attendance is not required on Fridays",
    ", office optional the other days",
    "; office not required on Mondays",
]
REMOTE_DAY_CLAUSES = [", WFH 2 days a week", "; work from home 2 days", ", remote 1 day a week"]
REMOTE_TEMPLATES = ["Remote", "Remote-first", "Fully remote", "We are a remote company"]
OPTIONAL_PRESENCE_CLAUSES = [
    ", with optional in-person days",
    "; RTO not required",
    " (no in-person requirement)",
    "; in-person attendance is optional",
    ", in-person optional",
    "; no RTO plans",
    "; office optional",
]
REMOTE_COUNT_TEMPLATES = [
    "Hybrid, WFH {n} days a week",
    "Hybrid: work from home {n} days per week",
    "Employees can work remotely up to {n} days per week",
    "Work from home {n} days, office the rest",
    "Hybrid - remote up to {n} days a week",
    "Hybrid, with {n} days of remote work per week",
]


@pytest.mark.parametrize("template,clause", itertools.product(IN_OFFICE_TEMPLATES, REMOTE_EXCEPTION_CLAUSES))
def test_stated_office_requirement_with_a_remote_exception_is_five_days(template, clause):
    assert _rto_days(template + clause) == 5


@pytest.mark.parametrize("template,clause", itertools.product(IN_OFFICE_TEMPLATES, REMOTE_EXCEPTION_CLAUSES))
def test_stated_office_requirement_is_never_read_remote(template, clause):
    assert _rto_days(template + clause) != 0


@pytest.mark.parametrize(
    "template,clause",
    itertools.product(IN_OFFICE_COUNT_TEMPLATES, REMOTE_EXCEPTION_CLAUSES + OPTIONAL_OFFICE_CLAUSES + REMOTE_DAY_CLAUSES),
)
def test_a_stated_office_count_is_never_read_lower_or_as_remote(template, clause):
    text, stated = template
    days = _rto_days(text + clause)
    # "WFH 2 days" after a count may read as that count's remote days: unknown, never lower.
    assert days is None or days == stated, (text + clause, days)
    if clause not in REMOTE_DAY_CLAUSES:
        assert days == stated, text + clause


@pytest.mark.parametrize("template,clause", itertools.product(IN_OFFICE_TEMPLATES, OPTIONAL_OFFICE_CLAUSES))
def test_an_optional_office_clause_on_some_days_never_reads_an_office_policy_as_remote(template, clause):
    assert _rto_days(template + clause) != 0


@pytest.mark.parametrize("template,clause", itertools.product(REMOTE_TEMPLATES, OPTIONAL_PRESENCE_CLAUSES))
def test_remote_with_optional_in_person_or_rto_never_reads_five_days(template, clause):
    assert _rto_days(template + clause) == 0


@pytest.mark.parametrize("template,n", itertools.product(REMOTE_COUNT_TEMPLATES, [1, 2, 3, 4]))
def test_a_count_of_remote_days_is_never_read_as_office_days(template, n):
    assert _rto_days(template.format(n=n)) is None


def test_two_counts_read_the_office_count():
    assert _rto_days("Hybrid, 3 days in office, 2 days remote") == 3
    assert _rto_days("2 days remote, 3 days in office") is None


@pytest.mark.parametrize(
    "text",
    ["Office not optional", "Onsite not optional", "Office is not optional", "In-office never optional"],
)
def test_office_not_optional_is_five_days(text):
    assert _rto_days(text) == 5


@pytest.mark.parametrize(
    "text",
    ["Not a remote position", "Remote isn't an option", "We do not offer remote work", "Remote: no", "No fully remote roles"],
)
def test_negated_remote_in_any_form_is_never_zero(text):
    assert _rto_days(text) is None


def test_a_count_in_one_clause_stands_when_another_clause_is_occasional_or_flexible():
    assert _rto_days("4 days in office; occasional remote Fridays") == 4
    assert _rto_days("3 days in office. Flexible hours.") == 3
    # structural mixes are still unknown
    assert _rto_days("Remote or hybrid; 3 days in office when hybrid") is None
    # two different counts in different clauses are unknown
    assert _rto_days("3 days in office; flexible. Or 4 days in office, occasional") is None


def test_optional_wording_on_remote_does_not_relax_a_stated_office_requirement():
    assert _rto_days("Office required, optional remote Fridays") == 5
    assert _rto_days("Onsite, optional remote Fridays") == 5
    assert _rto_days("Remote OK, office required Tuesdays") is None


def test_in_person_extras_on_a_remote_company_are_unknown():
    assert _rto_days("Remote-first; in-person meetups") is None
    assert _rto_days("Remote-first; in-person attendance required") == 5


def test_a_single_in_office_day_is_one():
    assert _rto_days("1 in-office day") == 1
    assert matching_reading("rto_policy", "1 in-office day") == "matching reads this as: hybrid (1 in-office day)"
