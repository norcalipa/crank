# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Allowlisted assistant UI actions (issue #484)."""
import pytest

from crank.agents.job_search import actions
from crank.agents.job_search.errors import InvalidActionError, InvalidModelOutputError

OPEN = {"type": "open_company", "organization_id": 12}
FILTER = {"type": "propose_filters", "target": "rankings", "filters": {"rto_policy": "R"}}
COMPARE = {"type": "compare_companies", "organization_ids": [3, 7]}


def test_valid_actions_round_trip_to_wire():
    parsed = actions.parse_actions([OPEN, FILTER, COMPARE])
    assert actions.to_wire(parsed) == [OPEN, FILTER, COMPARE]


def test_filter_target_defaults_and_vesting_accepted():
    parsed = actions.parse_actions(
        [{"type": "propose_filters", "filters": {"accelerated_vesting": True, "rto_policy": "H"}}]
    )
    assert parsed[0]["target"] == "rankings"
    assert parsed[0]["filters"] == {"accelerated_vesting": True, "rto_policy": "H"}


@pytest.mark.parametrize("empty", [None, []])
def test_absent_actions_are_empty(empty):
    assert actions.parse_actions(empty) == ()


@pytest.mark.parametrize("raw", [
    "open_company",
    {"type": "open_company", "organization_id": 1},
    [OPEN, FILTER, COMPARE, OPEN],
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
def test_hostile_actions_are_rejected(raw):
    with pytest.raises(InvalidModelOutputError):
        actions.parse_actions(raw)


def test_references_must_be_exposed():
    parsed = actions.parse_actions([OPEN, FILTER, COMPARE])
    actions.validate_action_references(parsed, {12, 3, 7})
    with pytest.raises(InvalidActionError):
        actions.validate_action_references(parsed, {12, 3})
    with pytest.raises(InvalidActionError):
        actions.validate_action_references(parsed, {3, 7})
    actions.validate_action_references(actions.parse_actions([FILTER]), set())
