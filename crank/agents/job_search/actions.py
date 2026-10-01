# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Typed, allowlisted assistant UI actions (issue #484).

The model may propose a small, fixed vocabulary of actions that the browser
renders as explicit buttons. Actions carry **ids and enums only**: no URLs,
free text, selectors or field names chosen by the model. Every action is
schema-checked here and reference-checked against the organization ids the
server exposed for the turn. This module is a frozen, code-owned allowlist and
deliberately has no registration API.
"""
from __future__ import annotations

from typing import Any

from crank.agents.job_search import tools
from crank.agents.job_search.errors import InvalidActionError, InvalidModelOutputError

OPEN_COMPANY = "open_company"
PROPOSE_FILTERS = "propose_filters"
COMPARE_COMPANIES = "compare_companies"

ALLOWED_ACTION_TYPES = frozenset({OPEN_COMPANY, PROPOSE_FILTERS, COMPARE_COMPANIES})
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


def parse_actions(raw: Any) -> tuple[dict, ...]:
    """Schema-check a model ``actions`` value; return canonical action dicts.

    Raises :class:`InvalidModelOutputError` for anything outside the
    allowlist: unknown types, extra keys, wrong types, URLs, or too many.
    """
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise InvalidModelOutputError("model output 'actions' must be a list")
    if len(raw) > MAX_ACTIONS:
        raise InvalidModelOutputError(f"model output has more than {MAX_ACTIONS} actions")
    parsed = []
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidModelOutputError("each action must be an object")
        action_type = item.get("type")
        parser = _PARSERS.get(action_type) if isinstance(action_type, str) else None
        if parser is None:
            raise InvalidModelOutputError("action type is not allowed")
        parsed.append(parser(item))
    return tuple(parsed)


def validate_action_references(actions, exposed_ids) -> None:
    """Reject any action naming an organization id the server did not expose."""
    exposed = frozenset(exposed_ids)
    for action in actions:
        if action["type"] == OPEN_COMPANY:
            referenced = [action["organization_id"]]
        elif action["type"] == COMPARE_COMPANIES:
            referenced = action["organization_ids"]
        else:
            continue
        unknown = [i for i in referenced if i not in exposed]
        if unknown:
            raise InvalidActionError(
                "model action referenced organization IDs not exposed by the server: "
                + ", ".join(str(i) for i in unknown)
            )


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
