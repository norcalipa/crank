# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Typed, allowlisted assistant UI actions (issue #484).

The model may propose a small, fixed vocabulary of actions that the browser
renders as explicit buttons. Actions carry **ids and enums only**: no URLs,
free text, selectors or field names chosen by the model. Every action is
schema-checked here and reference-checked against the organization ids the
server exposed for the turn; invalid actions are dropped, never fatal. This module is a frozen, code-owned allowlist and
deliberately has no registration API.
"""
from __future__ import annotations

from typing import Any

from crank.agents.job_search import tools
from crank.agents.job_search.errors import InvalidModelOutputError

OPEN_COMPANY = "open_company"
PROPOSE_FILTERS = "propose_filters"
COMPARE_COMPANIES = "compare_companies"

MAX_ACTIONS = 3
MIN_COMPARE_COMPANIES = 2
MAX_COMPARE_COMPANIES = 4
FILTER_TARGET = "rankings"

#: Filter keys the model may propose and the values each accepts. ``search`` is
#: deliberately absent: free text must never reach a URL.
FILTER_ALLOWLIST = {
    "rto_policy": tools.RTO_POLICY_VALUES,
    "accelerated_vesting": frozenset({True}),
}


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_open_company(raw: dict) -> dict:
    if set(raw) != {"type", "organization_id"}:
        raise InvalidModelOutputError("open_company action has unexpected keys")
    if not _is_id(raw["organization_id"]):
        raise InvalidModelOutputError("open_company organization_id must be a positive integer")
    return {"type": OPEN_COMPANY, "organization_id": raw["organization_id"]}


def _parse_propose_filters(raw: dict) -> dict:
    if not set(raw) <= {"type", "target", "filters"} or "filters" not in raw:
        raise InvalidModelOutputError("propose_filters action has unexpected keys")
    if raw.get("target", FILTER_TARGET) != FILTER_TARGET:
        raise InvalidModelOutputError("propose_filters target is not allowed")
    filters = raw["filters"]
    if not isinstance(filters, dict) or not filters:
        raise InvalidModelOutputError("propose_filters filters must be a non-empty object")
    clean: dict[str, Any] = {}
    for key in sorted(filters, key=str):
        allowed = FILTER_ALLOWLIST.get(key) if isinstance(key, str) else None
        if allowed is None:
            raise InvalidModelOutputError("propose_filters filter key is not allowed")
        value = filters[key]
        try:
            ok = value in allowed
        except TypeError:
            ok = False
        # ``1 in {True}`` is true in Python; require the exact type.
        if not ok or (key == "accelerated_vesting" and value is not True):
            raise InvalidModelOutputError("propose_filters filter value is not allowed")
        clean[key] = value
    return {"type": PROPOSE_FILTERS, "target": FILTER_TARGET, "filters": clean}


def _parse_compare_companies(raw: dict) -> dict:
    if set(raw) != {"type", "organization_ids"}:
        raise InvalidModelOutputError("compare_companies action has unexpected keys")
    ids = raw["organization_ids"]
    if (
        not isinstance(ids, list)
        or not MIN_COMPARE_COMPANIES <= len(ids) <= MAX_COMPARE_COMPANIES
        or not all(_is_id(i) for i in ids)
        or len(set(ids)) != len(ids)
    ):
        raise InvalidModelOutputError(
            "compare_companies organization_ids must be 2-4 unique positive integers"
        )
    return {"type": COMPARE_COMPANIES, "organization_ids": list(ids)}


_PARSERS = {
    OPEN_COMPANY: _parse_open_company,
    PROPOSE_FILTERS: _parse_propose_filters,
    COMPARE_COMPANIES: _parse_compare_companies,
}


#: Low-cardinality reasons an action may be dropped (telemetry values).
DROP_NOT_A_LIST = "actions_not_a_list"
DROP_NOT_AN_OBJECT = "action_not_an_object"
DROP_UNKNOWN_TYPE = "unknown_type"
DROP_BAD_SCHEMA = "bad_schema"
DROP_UNEXPOSED_ID = "unexposed_id"
DROP_OVER_LIMIT = "over_limit"
DROP_STALE_CONTEXT = "stale_context"


def _parse_one(item: Any) -> dict:
    """Schema-check one action; raise :class:`InvalidModelOutputError` if it is not allowed."""
    if not isinstance(item, dict):
        raise InvalidModelOutputError("each action must be an object")
    action_type = item.get("type")
    parser = _PARSERS.get(action_type) if isinstance(action_type, str) else None
    if parser is None:
        raise InvalidModelOutputError("action type is not allowed")
    return parser(item)


def _referenced_ids(action: dict) -> list[int]:
    if action["type"] == OPEN_COMPANY:
        return [action["organization_id"]]
    if action["type"] == COMPARE_COMPANIES:
        return list(action["organization_ids"])
    return []


def sanitize_actions(
    raw: Any, exposed_ids=None, *, stale: bool = False
) -> tuple[tuple[dict, ...], tuple[str, ...]]:
    """Return ``(valid_actions, drop_reasons)`` for a model ``actions`` value.

    Actions are advisory UI suggestions, so a bad one never fails the turn:
    anything outside the allowlist, over the cap, or naming an organization id
    the server did not expose (when ``exposed_ids`` is given) is dropped and a
    low-cardinality reason is returned for telemetry. With ``stale=True`` (the
    user's view is outdated) every otherwise-valid action is dropped as
    ``stale_context``. ``compare_companies`` is
    accepted here but not advertised in the prompt until #490.
    """
    if raw is None:
        return (), ()
    if not isinstance(raw, (list, tuple)):
        return (), (DROP_NOT_A_LIST,)
    exposed = None if exposed_ids is None else frozenset(exposed_ids)
    kept: list[dict] = []
    reasons: list[str] = []
    for item in raw:
        if len(kept) >= MAX_ACTIONS:
            reasons.append(DROP_OVER_LIMIT)
            continue
        try:
            action = _parse_one(item)
        except InvalidModelOutputError:
            if not isinstance(item, dict):
                reasons.append(DROP_NOT_AN_OBJECT)
            elif not isinstance(item.get("type"), str) or item["type"] not in _PARSERS:
                reasons.append(DROP_UNKNOWN_TYPE)
            else:
                reasons.append(DROP_BAD_SCHEMA)
            continue
        if exposed is not None and any(i not in exposed for i in _referenced_ids(action)):
            reasons.append(DROP_UNEXPOSED_ID)
            continue
        if stale:
            reasons.append(DROP_STALE_CONTEXT)
            continue
        kept.append(action)
    return tuple(kept), tuple(reasons)


def to_wire(actions) -> list[dict]:
    """JSON-serialisable copy of validated actions for the HTTP payload."""
    wire = []
    for action in actions:
        copy = dict(action)
        if "filters" in copy:
            copy["filters"] = dict(copy["filters"])
        if "organization_ids" in copy:
            copy["organization_ids"] = list(copy["organization_ids"])
        wire.append(copy)
    return wire
