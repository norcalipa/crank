# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""The office-policy reading corpus (issue #537, stage 1).

Stage 1 pins what ``_rto_days`` returns today and checks that the proposed
reader (a frozen copy in the report command) agrees with the proposed column.
No behaviour changes: stage 2 switches ``test_today_is_pinned`` to the proposed
column when the owner approves it.
"""
import pytest

from crank.agents.jobs.matching import _mode_from_rto, _rto_days
from crank.management.commands.rto_reading_report import _mode, proposed_rto_days
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
def test_today_is_pinned(text, today, proposed, category, source):
    assert _rto_days(text) == today


@pytest.mark.parametrize("text,today,proposed,category,source", ROWS)
def test_frozen_proposed_reader_matches_the_proposed_column(text, today, proposed, category, source):
    assert proposed_rto_days(text) == proposed


@pytest.mark.parametrize("text,today,proposed,category,source", ROWS)
def test_matching_reading_never_raises(text, today, proposed, category, source):
    reading = matching_reading("rto_policy", text)
    assert reading.startswith("matching ")
    assert _mode_from_rto(text) in {"remote", "hybrid", "in-office", None}


def test_proposed_never_reads_stated_in_office_policies_as_remote():
    """The #525 failure mode, stated against the proposed column."""
    in_office = {"office-first", "in-office-count", "in-office-weekdays", "onsite-spelling"}
    for text, _today, proposed, category, _source in CORPUS:
        if category in in_office:
            assert proposed != 0, text
        if category == "negated-remote":
            assert proposed is None, text
        if category in {"mixed", "occasional", "empty"}:
            assert proposed is None, text
        if category == "remote-incidental-office":
            assert proposed == 0, text


def test_proposed_mode_by_range():
    assert [_mode(d) for d in (None, 0, 1, 2, 3, 4, 5)] == [
        None, "remote", "hybrid", "hybrid", "hybrid", "hybrid", "in-office",
    ]
