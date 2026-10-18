# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The office-policy reading corpus (issue #537).

``_rto_days`` returns the owner-approved ``proposed`` column for every row. The
``today`` column stays as the record: the report command keeps a frozen copy of
the earlier reader and this module checks it still reproduces that column.
"""
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
        assert matching_reading("rto_policy", text) == f"matching reads this as: {label} ({days} in-office days)"
    assert matching_reading("rto_policy", "It depends") == "matching cannot read this (unknown)"
