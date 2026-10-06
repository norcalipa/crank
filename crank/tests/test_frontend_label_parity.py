# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Funding-round and RTO labels are the same words on every surface (issue #473)."""

import re
from pathlib import Path

from django.conf import settings

from crank.agents.jobs import matching
from crank.models.organization import Organization
from crank.services import job_matching

LABELS_TS = Path(settings.BASE_DIR) / "static" / "js" / "labels.ts"
STATIC_JS = Path(settings.BASE_DIR) / "static" / "js"

_MAP_RE = r"export const %s: Record<string, string> = \{\n(?P<body>(?:    [A-Z]: '[^'\n]*',\n)+)\};\n"
_ENTRY_RE = re.compile(r"    ([A-Z]): '([^'\n]*)',\n")


def _ts_map(name: str) -> dict[str, str]:
    """Parse one flat ``Record<string, string>`` literal; anything else fails."""
    matches = re.findall(_MAP_RE % re.escape(name), LABELS_TS.read_text(encoding="utf-8"))
    assert len(matches) == 1, f"expected exactly one strict {name} literal in labels.ts"
    entries = _ENTRY_RE.findall(matches[0])
    assert len(entries) == matches[0].count("\n")
    parsed = dict(entries)
    assert len(parsed) == len(entries), f"duplicate key in {name}"
    return parsed


def _choices(enum) -> dict[str, str]:
    return {code: str(label) for code, label in enum.choices}


def test_funding_round_labels_match_the_model():
    assert _ts_map("FUNDING_ROUND_LABELS") == _choices(Organization.FundingRound)


def test_rto_policy_labels_match_the_model():
    assert _ts_map("RTO_POLICY_LABELS") == _choices(Organization.RTOPolicy)


def test_server_reason_labels_derive_from_the_model():
    funding = _choices(Organization.FundingRound)
    rto = _choices(Organization.RTOPolicy)
    for module in (matching, job_matching):
        assert module._FUNDING_LABELS == funding
        assert module._RTO_LABELS == rto
        assert module._MODE_LABELS == {
            "remote": rto["R"], "hybrid": rto["H"], "in-office": rto["O"],
        }
    assert funding["X"] == "Series G or Later"
    assert rto["O"] == "In-Office"


def test_no_component_keeps_a_private_funding_or_rto_map():
    """The labels live in labels.ts only; the chat's wrong maps stay deleted."""
    offenders = []
    for path in sorted(STATIC_JS.rglob("*.ts*")):
        if path.name == "labels.ts" or ".test." in path.name:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"['\"]?[SABCDEFXOP]['\"]?: '(Seed|Series [A-G][^']*|Late Stage|IPO|Pre-IPO|Other Private|Public)'", text):
            offenders.append(f"{path.name}: funding-round map")
        if re.search(r"['\"]?[RHO]['\"]?: '(Remote|Hybrid|In-[Oo]ffice|On-site)'", text):
            offenders.append(f"{path.name}: RTO map")
    assert offenders == []
