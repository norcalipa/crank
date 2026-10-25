# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the fail-closed office-policy reader (issue #537).

Family tests pin each template and its near misses. The properties then show
that no text outside the templates reads a number: P1 (closed vocabulary), P2
(an out-of-vocabulary word inserted anywhere) and P3 (two statements that read
differently, joined).
"""
import itertools
import random
import time

import pytest

from crank.agents.jobs import office_policy
from crank.agents.jobs.office_policy import (
    MAX_CHARS,
    STOP_WORDS,
    TEMPLATE_WORDS,
    _weekday_days,
    office_days,
    office_template,
)
from crank.tests.agents.rto_policy_corpus import CORPUS
from crank.tests.agents.rto_policy_probes import PROBES


# --------------------------------------------------------------- exact (X)
@pytest.mark.parametrize(
    "text,days",
    [
        ("r", 0), ("Remote", 0), ("  REMOTE  ", 0), ("h", 3), ("Hybrid", 3), ("O", 5), ("In_Office", 5),
        ("in-office", 5), ("In Office", 5), ("Onsite", 5), ("on-site", 5), ("On Site", 5),
    ],
)
def test_exact_codes_and_labels(text, days):
    assert office_days(text) == days
    assert office_template(text) == "exact"


@pytest.mark.parametrize("text", ["", "   ", "TBD", "office hubs", "Remote OK", "R remote", "x", "h h"])
def test_other_short_values_are_unknown(text):
    assert office_days(text) is None
    assert office_template(text) is None


def test_none_and_non_strings_read_as_text():
    assert office_days(None) is None
    assert office_days(5) is None
    assert office_days(["Remote"]) == 0  # read as its text, like every other reader of the value

    class Label:
        def __str__(self):
            return "Re​mote"

    assert office_days(Label()) == 0


def test_hidden_characters_cannot_split_or_hide_a_word():
    assert office_days("Re​mote") == 0
    assert office_days("Rem‮ote") == 0
    assert office_days("Hy​brid, 3 days") == 3
    assert office_days("Ｒemote") == 0
    assert office_days("Remote\n") == 0
    # tab, newline and carriage return are whitespace, not hidden characters
    assert office_days("Fully\nremote") == 0
    assert office_days("In office\n3 days a week") == 3
    assert office_days("Hybrid\t3 days in office") == 3


# ------------------------------------------------------------ T1 remote (0)
@pytest.mark.parametrize(
    "text",
    [
        "Fully remote", "100% remote", "Remote-first", "Remote only", "Remote-first company", "Remote worldwide",
        "Remote (US)", "Remote work in the United States only", "Completely remote team",
        "Not hybrid; fully remote", "No hybrid option; remote-first", "We are a fully remote company",
        "All employees work remotely", "Remote-first; RTO not required", "Remote first, no office",
        "Remote-first with optional office hubs", "Remote, with an office in NYC",
        "Remote-first; optional office space in SF", "Remote first, office hubs in NYC",
        "Remote-first, offices in 3 cities", "Remote, headquartered in Austin", "Remote-first, async by default",
        "Remote; employees may use our office in Denver", "Remote-first with coworking stipend",
        "RTO policy: remote-first",
    ],
)
def test_remote_statements_read_zero(text):
    assert office_days(text) == 0, text
    assert office_template(text) in {"remote", "optional-office"}


@pytest.mark.parametrize(
    "text",
    [
        "Remote OK", "Remote (Germany)", "Remote, office required", "Remote-first; in-person meetups",
        "We are no longer remote", "Remote 2 days a week", "Not remote", "No remote", "Remote isn't an option",
        "We don't do remote", "Remote, with an office in nyc", "Remote, with an office in Austin H",
        "Remote, with an office in NYC required", "Remote, with an office in Remote-Friendly",
        "Remote, with an office in Daily-ish", "Remote-first, an office in NYC daily",
        "Remote, with an office in the", "Remote, with an office in",
    ],
)
def test_remote_near_misses_are_unknown(text):
    assert office_days(text) is None, text


# The PLACE slot, one rule per test.
def test_a_lowercase_place_is_unknown():
    assert office_days("Remote, with an office in NYC") == 0
    assert office_days("Remote, with an office in nyc") is None


def test_a_one_letter_place_token_is_unknown():
    assert office_days("Remote, with an office in Austin H") is None
    assert office_days("Remote, with an office in Austin Texas") == 0


def test_a_stop_word_is_never_a_place_token():
    assert office_days("Remote, with an office in NYC required") is None
    assert office_days("Remote, with an office in NYC Daily") is None


def test_a_hyphenated_place_token_is_unknown():
    assert office_days("Remote, with an office in Remote-Friendly") is None
    assert office_days("Remote, with an office in Daily-ish") is None


def test_places_join_with_and_or_and_count_phrases_name_places():
    assert office_days("Remote-first, offices in 3 cities") == 0
    assert office_days("Remote-first, offices in five countries") == 0
    assert office_days("Remote, offices in London and Berlin") == 0
    assert office_days("Remote, offices in the Bay Area or San Francisco") == 0
    assert office_days("Remote, offices in London, Berlin and Paris") == 0
    assert office_days("Remote, offices in London and berlin") is None
    assert office_days("Remote, offices in 3 cities and Berlin") is None


# ----------------------------------------------------- T2 optional office (0)
@pytest.mark.parametrize(
    "text",
    [
        "No office required", "optional office space", "Office is optional", "RTO not required",
        "No RTO mandate", "In-person is optional", "In person: optional", "Zero office requirement",
        "No mandatory office", "Office attendance isn't required", "Return to office is not needed",
        "Work policy: no office required",
    ],
)
def test_optional_office_reads_zero(text):
    assert office_days(text) == 0, text
    assert office_template(text) == "optional-office"


@pytest.mark.parametrize(
    "text",
    [
        "Office optional for engineers, required for sales", "Office optional? No.", "No RTO",
        "Office optional for the first month, then required", "Optional", "Office is optional on Mondays",
    ],
)
def test_optional_office_near_misses_are_unknown(text):
    assert office_days(text) is None, text


# ------------------------------------------------------- T3 zero days (0)
def test_zero_days_in_office_reads_zero():
    assert office_days("0 days in office") == 0
    assert office_days("Zero days in the office") == 0
    assert office_days("No days in office") == 0
    assert office_template("0 days in office") == "zero-days"
    assert office_days("0 days") is None


# ------------------------------------------------------- T4 day count (n)
@pytest.mark.parametrize(
    "text,days",
    [
        ("Hybrid, 3 days in office", 3), ("In office 2 days per week", 2), ("RTO: 3 days a week", 3),
        ("Days in office: 4", 4), ("In office twice a week", 2), ("Hybrid 3 days", 3), ("Hybrid, two days a week", 2),
        ("3d in-office", 3), ("3 d in office", 3), ("3x a week in office", 3), ("In office 3x per week", 3),
        ("Hybrid, 3-day a week", 3), ("In office once a week", 1), ("1 in-office day", 1),
        ("Weekly days in office 2", 2), ("4 office days a week", 4), ("Five days in office, no exceptions", 5),
        ("In office 4 days a week (office optional on Fridays)", 4),
        ("In-office 4 days a week; office attendance is not required on Fridays", 4),
        ("In office 3 days, office optional the other days", 3),
        ("Return-to-office 4 days a week, remote Fridays", 4),
        ("In office 3 days a week; remote Mon and Fri", 3),
        ("Return-to-office 4 days a week, remote Mon Fri", 4),  # a bare pair of weekdays in the tail
        ("In office 4 days a week, office optional Mondays, Fridays", 4),
        ("Not remote; in office 4 days a week", 4),
        ("No remote work. Office attendance required 5 days a week", 5),
        ("Hybrid with 2 days in office", 2),
        ("RTO policy: in office 3 days a week", 3),
        ("In office 3 days a week required", 3),
        ("Office required 4 days a week", 4),
        ("3 days a week in office; remote by exception", 3),
    ],
)
def test_day_counts(text, days):
    assert office_days(text) == days, text
    if days is not None:
        assert office_template(text) in {"count", "in-office"}


@pytest.mark.parametrize(
    "text",
    [
        "3 days", "Four days a week", "Hybrid, 2-3 days in office", "Hybrid, WFH 2 days a week",
        "Hybrid, 3 days in office, 2 days remote", "Remote-first; in-person 2 days a month", "3x in office",
        "3-day onsite", "Hybrid, 6 days in office", "In office 3 days a week or 4", "Hybrid, 3 days remote",
        "In office 3 days a week; office optional", "In office 3 days a week; office optional on Fridays and more",
        "Hybrid, 3 days in office, flexible",
    ],
)
def test_day_count_near_misses_are_unknown(text):
    assert office_days(text) is None, text


# ------------------------------------------------------ weekday spans (n)
@pytest.mark.parametrize(
    "words,days",
    [
        ("tuesday", 1), ("tuesday-thursday", 3), ("monday-thursday", 5), ("monday through friday", 5),
        ("mon to thu", 5), ("mon thru wed", 3), ("tuesdays and thursdays", 2), ("tues wed thurs", 3),
        ("mon wed fri", 3), ("on tuesday and thursday", 2), ("monday-wednesday and friday", 5),
        ("tuesday wednesday", 2), ("mon tue wed thu", 5),
        ("tuesday thursday", 2), ("friday monday", 2), ("monday friday", 2),  # a bare gap is a list
        ("wednesday monday-friday", 5), ("monday-tuesday thursday", 3), ("mon-wed fri", 5),
        ("monday-wednesday friday", 5), ("tuesday-thursday friday", 5),
    ],
)
def test_weekday_count_rule(words, days):
    assert _weekday_days(words) == days


@pytest.mark.parametrize(
    "words",
    [
        "thursday-tuesday",  # backwards
        "monday-wednesday-friday",  # chained span
        "monday or tuesday",  # "or" names a choice, not a list
        "monday tuesday wednesday or thursday",
        "tuesday ish thursday",  # not a connector
        "tuesday and or thursday",  # two connectors
        "tuesday - and thursday",
        "tuesday-fr\u0131day",  # text after the last weekday that is not a weekday
    ],
)
def test_weekday_count_rule_refuses_what_it_cannot_count(words):
    assert _weekday_days(words) is None


# --------------------------------------------------- T5 double negative (5)
@pytest.mark.parametrize(
    "text",
    [
        "Office not optional", "Office isn't optional", "Office is not optional", "Onsite not optional",
        "In-office never optional", "Remote not an option; office not optional", "In person is never voluntary",
        "Not hybrid: office not optional", "Office attendance is not optional",
    ],
)
def test_double_negatives_read_five(text):
    assert office_days(text) == 5, text
    assert office_template(text) == "double-negative"


def test_a_double_negative_near_miss_is_unknown():
    assert office_days("Being in the office isn't optional") is None
    assert office_days("Office not optional on Fridays") is None


# ------------------------------------------------------- T6 in-office (5)
@pytest.mark.parametrize(
    "text",
    [
        "Office-first; remote by exception", "In office Monday-Friday", "Office", "The office", "In person", "RTO",
        "Return to office", "On site, no remote", "Office required, optional remote Fridays",
        "Everyone works from our Austin office; we don't offer remote positions.",
        "Office attendance: required. Remote work: by exception.", "Not hybrid: in office",
        "Fully in office", "In office full time", "Office based only", "Onsite at HQ", "In-person company",
        "Employees work from the office", "You must be in the office", "Return to office mandate",
        "Office required; remote only with VP approval", "In office, no exceptions",
        "Everyone works in our San Francisco office five days a week",
        "Employees are expected to be in the San Francisco office daily",
    ],
)
def test_in_office_statements_read_five(text):
    assert office_days(text) == 5, text


@pytest.mark.parametrize(
    "text",
    [
        "Primarily in the office, remote on Fridays", "You will work from our office in London, with remote Fridays",
        "Office optional for the first month, then required", "Office required unless approved; remote optional",
        "Back in the office full time; remote exceptions for caregivers", "Office in London",
        "Office required, remote Fridays occasionally", "Mostly in office",
    ],
)
def test_in_office_near_misses_are_unknown(text):
    assert office_days(text) is None, text


# --------------------------------------------------- T7 hybrid, no count (3)
@pytest.mark.parametrize(
    "text",
    ["Hybrid schedule", "Hybrid, details shared in interview", "Hybrid in San Francisco", "Hybrid work model", "Hybrid role"],
)
def test_hybrid_without_a_count_reads_three(text):
    assert office_days(text) == 3
    assert office_template(text) == "hybrid"


@pytest.mark.parametrize("text", ["Hybrid (flexible days)", "Hybrid or remote", "Hybrid remote", "Hybrid in san francisco"])
def test_hybrid_near_misses_are_unknown(text):
    assert office_days(text) is None


# ----------------------------------------------------- normalization, cap
def test_a_policy_label_may_lead_a_statement():
    assert office_days("RTO policy: Hybrid, 3 days in office") == 3
    assert office_days("Work model: remote-first") == 0
    assert office_days("Policy - fully remote") == 0


def test_a_lone_hyphen_is_dropped_but_a_word_hyphen_is_kept():
    assert office_days("Hybrid - one day a week in the office") == 1
    assert office_days("Hybrid -- 3 days in office") == 3
    assert office_days("on-site") == 5
    assert office_days("Hybrid -3 days in office") == 3  # a hyphen touching a word on one side only
    assert office_days("Remote-") == 0
    assert office_days("-Remote") == 0


@pytest.mark.parametrize(
    "text",
    [
        "Hybrid, f\u0131ve days in office", "Hybrid, F\u0130VE days in office", "\u0130n office Friday",
        "In office tw\u0131ce a week", "In office Fr\u0131day", "In office Tuesday-Fr\u0131day", "\u0130n office",
        "Remote f\u0131rst",
    ],
)
def test_turkish_dotless_and_dotted_i_read_unknown_and_never_raise(text):
    """re.IGNORECASE alone folds U+0131 and U+0130 onto i; the templates are ASCII-only."""
    assert office_days(text) is None
    assert office_template(text) is None


def test_a_place_name_may_use_non_ascii_letters():
    assert office_days("Remote first with an office in S\u00e3o Paulo") == 0


def test_a_value_longer_than_the_cap_is_unknown():
    text = "Remote " + "first " * 40
    assert office_days("Remote-first") == 0
    assert len(office_policy._normalize("Remote" + " " * 5 + "first")) < MAX_CHARS
    padded = "Remote-first, with an office in " + " ".join(["Austin"] * 60)
    assert len(office_policy._normalize(padded)) > MAX_CHARS
    assert office_days(padded) is None
    assert office_days(text) is None
    lead = "Remote first with an office in "
    at_cap = lead + "A" * (MAX_CHARS - len(lead))
    assert len(at_cap) == MAX_CHARS
    assert office_days(at_cap) == 0  # at the cap the templates still read it
    assert office_days(at_cap + "A") is None


def test_reads_are_cached_in_a_bounded_cache():
    assert office_policy._read.cache_info().maxsize == 8192
    assert office_days("Remote") == office_days("Remote")


def test_adversarial_long_values_stay_unknown_and_finish():
    for unit in ("Remote ", "in office ", "Austin ", "3 days ", "optional office "):
        text = unit * (500 // len(unit))
        assert office_days(text) is None
        assert office_days(text + "Remote") is None


def test_the_length_cap_and_the_place_bounds_are_pinned():
    assert MAX_CHARS == 240
    lead = "Remote first with an office in "
    assert office_days(f"{lead}Aa Bb Cc") == 0  # three tokens in a group, one group
    assert office_days(f"{lead}Aa Bb Cc Dd") is None
    assert office_days(f"{lead}Aa and Bb and Cc") == 0  # three groups
    assert office_days(f"{lead}Aa and Bb and Cc and Dd") is None


def test_adversarial_values_at_the_cap_reach_the_patterns_and_finish_fast():
    """Values at or under the cap are matched by every template; each read stays well under a second."""
    units = ["Remote ", "in office ", "Austin ", "3 days ", "optional office ", "Ab ", "and ", "Mon-", "Tue/", "a "]
    values = [(unit * 120)[:MAX_CHARS] for unit in units]
    values += [
        "Remote, with an office in " + "Ab " * 70 + "3",
        "Remote first with an office in " + "Ab and " * 30,
        "In office " + "Monday and " * 20 + "Friday",
        "Hybrid " + "Ab " * 77,
    ]
    for value in values:
        assert len(office_policy._normalize(value)) <= MAX_CHARS, value
        started = time.perf_counter()
        assert office_days(value) is None, value
        assert time.perf_counter() - started < 1.0, value


def test_families_that_disagree_read_unknown(monkeypatch):
    """M3: two templates that match the same text with different readings read unknown."""
    import re

    office_policy._read.cache_clear()
    try:
        assert office_days("Hybrid schedule") == 3
        office_policy._read.cache_clear()
        monkeypatch.setattr(
            office_policy, "TEMPLATES",
            [*office_policy.TEMPLATES, ("rival", 0, re.compile("hybrid schedule", re.IGNORECASE))],
        )
        assert office_days("Hybrid schedule") is None
        assert office_template("Hybrid schedule") is None
        # the same reading from two families is not a disagreement
        office_policy._read.cache_clear()
        monkeypatch.setattr(
            office_policy, "TEMPLATES",
            [*office_policy.TEMPLATES[:-1], ("same", 3, re.compile("hybrid schedule", re.IGNORECASE))],
        )
        assert office_days("Hybrid schedule") == 3
    finally:
        monkeypatch.undo()
        office_policy._read.cache_clear()


# ------------------------------------------------------------- properties
OOV_WORDS = """
after before but certain contractors could currently depends discontinued encouraged ended engineers
eventually except few flexible formerly generally half hires however ideally if initially later longer
managers maybe might month monthly most mostly new occasionally often part-time partially prefer preferred
primarily quarterly sales seldom should some sometimes soon starting stopped summer tbd temporarily then
typically unless until used usually varies virtual when would year
""".split()


def test_p1_the_out_of_vocabulary_words_are_outside_every_template_and_are_stop_words():
    assert len(OOV_WORDS) == 64
    assert not set(OOV_WORDS) & TEMPLATE_WORDS
    assert set(OOV_WORDS) <= STOP_WORDS  # so a Capitalized form is never a PLACE token


def _numeric_instances():
    texts = {text for text, _today, proposed, _category, _source in CORPUS if proposed is not None}
    texts |= {text for text, expected, _group in PROBES if expected is not None}
    return sorted(texts)


def _insertions(text, words):
    tokens = office_policy._normalize(text).split()
    for word in words:
        for form in (word, word.capitalize()):
            for at in range(len(tokens) + 1):
                yield " ".join(tokens[:at] + [form] + tokens[at:])


def test_p2_an_out_of_vocabulary_word_inserted_anywhere_makes_a_statement_unknown():
    instances = _numeric_instances()
    assert len(instances) > 250
    families = {}
    for text in instances:
        families.setdefault(office_template(text), text)
    assert {"exact", "remote", "optional-office", "zero-days", "count", "double-negative", "in-office", "hybrid"} <= set(
        families
    ) | {"zero-days"}
    # the full product for one instance per family ...
    for text in families.values():
        for inserted in _insertions(text, OOV_WORDS):
            assert office_days(inserted) is None, inserted
    # ... and a deterministic sample over every instance
    rng = random.Random(537)
    for _ in range(20_000):
        text = rng.choice(instances)
        word = rng.choice(OOV_WORDS)
        tokens = office_policy._normalize(text).split()
        at = rng.randint(0, len(tokens))
        form = word if rng.random() < 0.5 else word.capitalize()
        inserted = " ".join(tokens[:at] + [form] + tokens[at:])
        assert office_days(inserted) is None, inserted


def test_p3_two_statements_that_read_differently_are_unknown_when_joined():
    instances = _numeric_instances()
    readings = {text: office_days(text) for text in instances}

    def differ(a, b):
        ra, rb = readings[a], readings[b]
        return (ra == 0) != (rb == 0) or (office_template(a) == office_template(b) == "count" and ra != rb)

    shortest = sorted(instances, key=len)[:40]
    pairs = [(a, b) for a, b in itertools.permutations(shortest, 2) if differ(a, b)]
    rng = random.Random(537)
    for _ in range(20_000):
        a, b = rng.sample(instances, 2)
        if differ(a, b):
            pairs.append((a, b))
    assert len(pairs) > 5_000
    for a, b in pairs:
        for joiner in ("; ", ", "):
            joined = a + joiner + b
            assert office_days(joined) is None, joined


def test_p4_no_probe_outside_the_templates_reads_a_number():
    for text, expected, _group in PROBES:
        if expected is None:
            assert office_template(text) is None, text
        else:
            assert office_template(text) is not None, text
