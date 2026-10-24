# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The office-policy reading corpus and probe table (issue #537).

``_rto_days`` returns the owner-approved ``proposed`` column for every row and
the expected reading for every probe. The ``today`` column stays as the record:
the report command keeps a frozen copy of the earlier reader and this module
checks it still reproduces that column.
"""
import itertools

import pytest

from crank.agents.jobs.matching import _mode_for_days, _mode_from_rto, _rto_days
from crank.management.commands.rto_reading_report import legacy_rto_days
from crank.services.company_evidence import matching_reading
from crank.tests.agents.rto_policy_corpus import CORPUS
from crank.tests.agents.rto_policy_probes import PROBES

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
    """The #525 failure mode, stated against what the reader returns, by category."""
    never_remote = {
        "office-first", "in-office-count", "in-office-weekdays", "onsite-spelling",
        "office-required-remote-exception", "office-count-optional-clause", "double-negative-office",
    }
    never_office = {"office-optional", "remote-incidental-office", "remote-optional-in-person"}
    unknown = {"mixed", "occasional", "empty", "remote-day-count", "negated-remote-contraction"}
    assert (never_remote | never_office | unknown) <= {row[3] for row in CORPUS}
    for text, _today, _proposed, category, _source in CORPUS:
        days = _rto_days(text)
        if category in never_remote:
            assert days != 0, text
        if category in never_office:
            assert days != 5, text
        if category in unknown:
            assert days is None, text
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


def test_two_counts_are_unknown():
    """A count of office days beside a count of remote days is not a single statement."""
    assert _rto_days("Hybrid, 3 days in office, 2 days remote") is None
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


def test_a_count_beside_an_occasional_or_flexible_clause_is_unknown():
    assert _rto_days("4 days in office; occasional remote Fridays") is None
    assert _rto_days("3 days in office. Flexible hours.") is None
    # structural mixes are unknown too
    assert _rto_days("Remote or hybrid; 3 days in office when hybrid") is None
    # two different counts in different clauses are unknown
    assert _rto_days("3 days in office; flexible. Or 4 days in office, occasional") is None


def test_optional_wording_on_remote_does_not_relax_a_stated_office_requirement():
    assert _rto_days("Office required, optional remote Fridays") == 5
    assert _rto_days("Onsite, optional remote Fridays") == 5
    assert _rto_days("Remote OK, office required Tuesdays") is None


def test_in_person_extras_on_a_remote_company_are_unknown():
    assert _rto_days("Remote-first; in-person meetups") is None
    assert _rto_days("Remote-first; in-person attendance required") is None


def test_a_single_in_office_day_is_one():
    assert _rto_days("1 in-office day") == 1
    assert matching_reading("rto_policy", "1 in-office day") == "matching reads this as: hybrid (1 in-office day)"


# The probe table: every string from the PR #602 reviews, with its expected reading.
PROBE_ROWS = [pytest.param(text, expected, id=f"{i}") for i, (text, expected, _group) in enumerate(PROBES)]


def test_the_probe_table_is_complete_and_well_formed():
    assert len(PROBES) == 821
    assert len({text for text, _expected, _group in PROBES}) == len(PROBES)
    assert {group for _text, _expected, group in PROBES} == {"review", "working-set"}
    assert sum(group == "review" for _text, _expected, group in PROBES) == 398
    assert {expected for _text, expected, _group in PROBES} == {None, 0, 1, 2, 3, 4, 5}


@pytest.mark.parametrize("text,expected", PROBE_ROWS)
def test_every_probe_reads_its_expectation(text, expected):
    assert _rto_days(text) == expected


@pytest.mark.parametrize("text,expected", PROBE_ROWS)
def test_matching_reading_never_raises_for_a_probe(text, expected):
    assert matching_reading("rto_policy", text).startswith("matching ")
    assert _mode_from_rto(text) == _mode_for_days(expected)


def test_the_corpus_and_the_probe_table_agree_on_every_shared_text():
    corpus = {text: proposed for text, _today, proposed, _category, _source in CORPUS}
    shared = [(text, expected) for text, expected, _group in PROBES if text in corpus]
    assert len(shared) >= 100
    for text, expected in shared:
        assert corpus[text] == expected, text


# Weekday spans and lists under an office or in-person lead (owner rule, 2026-10-24):
# four or more weekdays read in-office (5); one to three read as that many days.
WEEKDAY_SPANS = [
    ("Monday", 1), ("Tuesday-Wednesday", 2), ("Tuesday-Thursday", 3), ("Monday-Thursday", 5),
    ("Monday-Friday", 5), ("Mon-Thu", 5), ("Tue-Thu", 3), ("Monday through Thursday", 5),
    ("Tuesday through Thursday", 3), ("Wednesday to Friday", 3), ("Mon thru Thu", 5),
]
WEEKDAY_LISTS = [
    ("Tuesdays", 1), ("Tuesdays and Thursdays", 2), ("Tues/Wed/Thurs", 3), ("Mon/Wed/Fri", 3),
    ("Mon, Tue, Wed and Thu", 5), ("Monday Tuesday Wednesday Thursday Friday", 5),
    ("Mondays, Wednesdays or Fridays", 3), ("Monday-Wednesday and Friday", 5),
]
WEEKDAY_LEADS = ["In office", "On-site", "Office required", "In person", "Hybrid: in office"]


@pytest.mark.parametrize("lead,span", itertools.product(WEEKDAY_LEADS, WEEKDAY_SPANS + WEEKDAY_LISTS))
def test_a_stated_weekday_span_or_list_reads_as_that_many_days(lead, span):
    weekdays, days = span
    assert _rto_days(f"{lead} {weekdays}") == days
    assert _rto_days(f"{lead} on {weekdays}") == days


@pytest.mark.parametrize("weekdays,days", WEEKDAY_SPANS + WEEKDAY_LISTS)
def test_a_weekday_span_or_list_before_the_office_location_reads_the_same(weekdays, days):
    assert _rto_days(f"{weekdays} in the office") == days
    assert _rto_days(f"Hybrid, {weekdays} in office") == days


@pytest.mark.parametrize("days", [1, 2, 3, 4, 5])
def test_every_span_length_reads_as_its_weekday_count(days):
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    expected = 5 if days >= 4 else days
    for start in range(0, 6 - days):
        last = start + days - 1
        span = names[start] if days == 1 else f"{names[start]}-{names[last]}"
        assert _rto_days(f"In office {span}") == expected, span
        assert _rto_days(f"In office {' and '.join(names[start:last + 1])}") == expected
        assert _rto_days(f"In office {'/'.join(n[:3] for n in names[start:last + 1])}") == expected


@pytest.mark.parametrize(
    "text",
    [
        "Tuesday-Thursday",  # no office or in-person lead
        "Remote Tuesday-Thursday",
        "Remote-first; in office Tuesday-Thursday",
        "Remote, in office Tuesdays",
        "In office Thursday-Tuesday",  # a backwards span
        "In office Tuesday-Wednesday-Thursday",  # a chained span
        "In office Tuesday Thursday",  # a span or a list after punctuation is dropped
        "In office Tuesday - Thursday",
        "In office Saturday-Sunday",
        "In office Tuesday-Thursday-ish",
        "In office most Tuesdays",
        "In office Tuesday-Thursday, flexible",
        "In office Tuesdays and Thursdays, remote the rest",
    ],
)
def test_a_weekday_span_outside_the_office_templates_is_unknown(text):
    assert _rto_days(text) is None


def test_an_adjacent_pair_of_weekdays_with_only_a_space_is_two_days_either_way():
    assert _rto_days("In office Tuesday Wednesday") == 2


def test_weekday_tails_keep_the_stated_count():
    assert _rto_days("In office Monday-Thursday; office attendance is not required on Fridays") == 5
    assert _rto_days("Hybrid: in office Tuesday-Thursday, office optional Monday and Friday") == 3
    assert _rto_days("In office Monday-Thursday; remote Fridays") == 5
    assert _rto_days("In office Tuesday-Thursday; remote Mon and Fri") == 3
