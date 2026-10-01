# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Allowlisted assistant UI actions (issue #484)."""
import pytest

from crank.agents.job_search import actions

OPEN = {"type": "open_company", "organization_id": 12}
FILTER = {"type": "propose_filters", "target": "rankings", "filters": {"rto_policy": "R"}}
COMPARE = {"type": "compare_companies", "organization_ids": [3, 7]}


def test_valid_actions_round_trip_to_wire():
    parsed, dropped = actions.sanitize_actions([OPEN, FILTER, COMPARE])
    assert dropped == ()
    assert actions.to_wire(parsed) == [OPEN, FILTER, COMPARE]


def test_filter_target_defaults_and_vesting_accepted():
    parsed, _ = actions.sanitize_actions(
        [{"type": "propose_filters", "filters": {"accelerated_vesting": True, "rto_policy": "H"}}]
    )
    assert parsed[0]["target"] == "rankings"
    assert parsed[0]["filters"] == {"accelerated_vesting": True, "rto_policy": "H"}


@pytest.mark.parametrize("empty", [None, []])
def test_absent_actions_are_empty(empty):
    assert actions.sanitize_actions(empty) == ((), ())


@pytest.mark.parametrize("raw", [
    "open_company",
    {"type": "open_company", "organization_id": 1},
    ["open_company"],
    [{"name": "run_shell", "args": ["rm"]}],
    [{"type": ["open_company"]}],
    [{"type": "navigate", "url": "https://evil.example"}],
    [{"type": "open_company", "organization_id": 1, "url": "https://evil.example"}],
    [{"type": "open_company"}],
    [{"type": "open_company", "organization_id": "1"}],
    [{"type": "open_company", "organization_id": True}],
    [{"type": "open_company", "organization_id": 0}],
    [{"type": "propose_filters", "filters": {}}],
    [{"type": "propose_filters"}],
    [{"type": "propose_filters", "filters": "R"}],
    [{"type": "propose_filters", "target": "jobs", "filters": {"rto_policy": "R"}}],
    [{"type": "propose_filters", "filters": {"search": "x"}}],
    [{"type": "propose_filters", "filters": {"rto_policy": "X"}}],
    [{"type": "propose_filters", "filters": {"rto_policy": ["R"]}}],
    [{"type": "propose_filters", "filters": {"rto_policy": "https://evil.example"}}],
    [{"type": "propose_filters", "filters": {"accelerated_vesting": 1}}],
    [{"type": "propose_filters", "filters": {"accelerated_vesting": False}}],
    [{"type": "propose_filters", "filters": {1: "R"}}],
    [{"type": "propose_filters", "filters": {"rto_policy": "R"}, "url": "/?x=1"}],
    [{"type": "compare_companies", "organization_ids": [1, 2], "url": "/x"}],
    [{"type": "compare_companies", "organization_ids": [1]}],
    [{"type": "compare_companies", "organization_ids": [1, 2, 3, 4, 5]}],
    [{"type": "compare_companies", "organization_ids": [1, 1]}],
    [{"type": "compare_companies", "organization_ids": "1,2"}],
    [{"type": "compare_companies", "organization_ids": [1, "2"]}],
])
def test_hostile_actions_are_dropped_not_fatal(raw):
    kept, dropped = actions.sanitize_actions(raw)
    assert kept == ()
    assert len(dropped) == 1


@pytest.mark.parametrize("raw,reason", [
    ("open_company", actions.DROP_NOT_A_LIST),
    (["open_company"], actions.DROP_NOT_AN_OBJECT),
    ([{"type": "navigate"}], actions.DROP_UNKNOWN_TYPE),
    ([{"type": "open_company"}], actions.DROP_BAD_SCHEMA),
])
def test_drop_reasons_are_specific(raw, reason):
    assert actions.sanitize_actions(raw)[1] == (reason,)


def test_one_bad_action_does_not_discard_the_good_ones():
    kept, dropped = actions.sanitize_actions([OPEN, {"type": "navigate"}, FILTER])
    assert kept == (OPEN, FILTER)
    assert dropped == (actions.DROP_UNKNOWN_TYPE,)


def test_actions_over_the_cap_are_dropped():
    kept, dropped = actions.sanitize_actions([OPEN, FILTER, COMPARE, OPEN])
    assert kept == (OPEN, FILTER, COMPARE)
    assert dropped == (actions.DROP_OVER_LIMIT,)


def test_references_must_be_exposed():
    kept, dropped = actions.sanitize_actions([OPEN, FILTER, COMPARE], {12, 3, 7})
    assert len(kept) == 3 and dropped == ()
    kept, dropped = actions.sanitize_actions([OPEN, FILTER, COMPARE], {12, 3})
    assert kept == (OPEN, FILTER) and dropped == (actions.DROP_UNEXPOSED_ID,)
    kept, dropped = actions.sanitize_actions([OPEN, FILTER, COMPARE], {3, 7})
    assert kept == (FILTER, COMPARE) and dropped == (actions.DROP_UNEXPOSED_ID,)
    assert actions.sanitize_actions([FILTER], set())[0] == (FILTER,)


def test_stale_context_drops_every_action():
    kept, dropped = actions.sanitize_actions([OPEN, FILTER], {12}, stale=True)
    assert kept == ()
    assert dropped == (actions.DROP_STALE_CONTEXT, actions.DROP_STALE_CONTEXT)
