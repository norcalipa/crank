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
RANKINGS_TEMPLATE = Path(settings.BASE_DIR) / "templates" / "crank" / "index.html"

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


_TERM_RE = re.compile(
    r"    (\w+): \{\n        label: '([^'\n]*)',\n        meaning: '([^'\n]*)',\n    \},\n"
)


def test_match_terms_read_the_same_in_how_ranking_works():
    """The job-card terms have one definition: labels.ts, repeated in the legend."""
    body = re.search(
        r"export const MATCH_TERMS: [^\n]* = \{\n(?P<body>.*?)\n\};\n",
        LABELS_TS.read_text(encoding="utf-8"),
        re.S,
    ).group("body") + "\n"
    terms = {key: (label, meaning) for key, label, meaning in _TERM_RE.findall(body)}
    assert set(terms) == {"sourced", "changed", "companyScore", "fit", "requirementCoverage"}
    assert _TERM_RE.sub("", body) == ""
    legend = RANKINGS_TEMPLATE.read_text(encoding="utf-8")
    for key in ("sourced", "changed"):
        label, meaning = terms[key]
        assert f'<span class="evidence-badge">{label}</span>: {meaning}</li>' in legend
    for key, heading in (
        ("companyScore", "Company score"),
        ("fit", "Fit"),
        ("requirementCoverage", "Requirement coverage"),
    ):
        label, meaning = terms[key]
        assert label.startswith(heading)
        assert re.search(
            r"<dt>%s</dt>\s*<dd>[^<]*%s</dd>" % (re.escape(heading), re.escape(meaning)), legend
        ), key
