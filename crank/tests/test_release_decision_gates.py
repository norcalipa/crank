# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Docs-to-code contract for the staged release decision gates (issue #492).

``release_gates:`` in ``docs/monitoring.yaml`` holds the evidence queries used
for staged release decisions. These tests bind every gate to the telemetry
allowlists from #482 (events, enum values), to the capability switch registry
and to the "Contextual assistant staged release (#492)" section of
``docs/rollout-gates.md``, and keep ``docs/usability-validation.md`` honest:
required sections, task and probe ids, the forbidden-data list, and every test
cited in the failure and retry matrix must exist.

The repository has no gate evaluator: a person runs the queries and applies the
procedure written in ``docs/monitoring.md``. ``RULES`` below is that procedure
as data — a test requires the documented table to match it row for row — and
``_outcome`` applies it, so the tests can show that no gate passes while its
sample floor is unset and that every gate can reach pass, hold and breach once
it is locked.

``PINNED_GATES`` restates what each gate measures (phase, switch, operator,
threshold, select expression, filter, alert list). It duplicates the YAML on
purpose: a one-line edit to a gate that changes what it measures must be made
in both places.
"""

import re
from datetime import timedelta
from pathlib import Path
from unittest import mock

import yaml
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from crank.models import AgentRun, JobListing, JobSourceCatalog
from crank.models.job_search import JobSearchConversation, JobSearchMessage
from crank.models.monitoring import ALLOWED_CAPABILITY_KEYS
from crank.models.preference import UserPreference
from crank.services import monitoring
from crank.views import assistant_status

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITORING_YAML = REPO_ROOT / "docs" / "monitoring.yaml"
MONITORING_DOC = REPO_ROOT / "docs" / "monitoring.md"
ROLLOUT_DOC = REPO_ROOT / "docs" / "rollout-gates.md"
USABILITY_DOC = REPO_ROOT / "docs" / "usability-validation.md"

GATE_NAMES = (
    "priorities-apply-success",
    "interactive-reply-success",
    "interactive-alerts-quiet",
    "interactive-time-to-first-result",
    "job-source-alerts-quiet",
    "job-matches-source-unavailable",
    "assistant-ready-share",
    "publication-outbox-age",
    "publication-to-match-lag",
    "matching-alerts-quiet",
    "evidence-stale-share",
)
PHASE_IDS = (
    "shell",
    "interactive_replies",
    "job_source",
    "publication",
    "match_recompute",
    "organization_crawl",
    "score_source",
)
COMMON_KEYS = {
    "name", "phase", "switch", "kind", "status", "window", "on_breach", "runbook",
    "operator", "threshold", "min_sample", "sample_nrql",
}
NRQL_KEYS = {"event", "nrql"}
QUIET_KEYS = {"alerts", "signal_event", "signal_filter", "policy_confirmed"}
FROM_CLAUSE = " FROM CrankOperation "
#: Switch of every phase in the phase table, including the gateless one.
PHASE_SWITCHES = {
    "shell": None,
    "interactive_replies": "interactive_agent",
    "job_source": "job_pipeline",
    "publication": "publication_consumer",
    "match_recompute": "match_recompute",
    "organization_crawl": "crawl_schedule",
    "score_source": "gather_scores",
}


def _nrql(phase, status, operator, threshold, window, select, where, sample):
    return {
        "kind": "nrql", "phase": phase, "switch": PHASE_SWITCHES[phase],
        "status": status, "operator": operator, "threshold": threshold,
        "window": window, "select": select, "where": where,
        "sample_select": sample,
    }


def _quiet(phase, window, signal, signal_filter, alerts):
    return {
        "kind": "alerts_quiet", "phase": phase, "switch": PHASE_SWITCHES[phase],
        "status": "provisional", "operator": "above", "threshold": 0,
        "window": window, "signal_event": signal, "signal_filter": signal_filter,
        "alerts": alerts,
    }


_ASSISTANT_POLLS = (
    "event_name = 'availability_state' AND surface = 'assistant_status'"
    " AND state != 'signed_out'"
)
_JOB_MATCH_POLLS = "event_name = 'availability_state' AND surface = 'job_matches'"
_RECOMPUTE_LAG = (
    "event_name = 'matching_batch' AND stage = 'match_recompute'"
    " AND publication_lag_count > 0"
)
_OUTBOX_GAUGE = (
    "event_name = 'pipeline_health' AND outbox_oldest_age_seconds IS NOT NULL"
)

#: What each gate measures. See the module docstring.
PINNED_GATES = {
    "priorities-apply-success": _nrql(
        "shell", "baseline_required", "below", None, "14 days",
        "filter(count(*), WHERE status = 'applied') / count(*)",
        "event_name = 'preference_decision' AND decision = 'apply'",
        "count(*)",
    ),
    "interactive-reply-success": _nrql(
        "interactive_replies", "provisional", "below", 0.90, "7 days",
        "filter(count(*), WHERE phase = 'replied')"
        " / filter(count(*), WHERE phase = 'attempted')",
        "event_name = 'assistant_turn'",
        "filter(count(*), WHERE phase = 'attempted')",
    ),
    "interactive-alerts-quiet": _quiet(
        "interactive_replies", "7 days", "interactive_call",
        "status = 'provider_succeeded'",
        ["cost-limit", "recurring-helpfulness-gaps", "repeated-failure"],
    ),
    "interactive-time-to-first-result": _nrql(
        "interactive_replies", "baseline_required", "above", None, "14 days",
        "percentile(seconds_to_first_result, 50)",
        "event_name = 'assistant_first_result'",
        "count(*)",
    ),
    "job-source-alerts-quiet": _quiet(
        "job_source", "7 days", "inventory_health", "enabled_sources IS NOT NULL",
        [
            "zero-enabled-sources", "zero-active-listings", "stale-inventory",
            "repeated-failures", "listing-collapse", "rejection-spike",
        ],
    ),
    "job-matches-source-unavailable": _nrql(
        "job_source", "provisional", "above", 0, "7 days",
        "filter(count(*), WHERE state IN ('no_source', 'source_disabled'))"
        " / count(*)",
        _JOB_MATCH_POLLS,
        "count(*)",
    ),
    "assistant-ready-share": _nrql(
        "job_source", "baseline_required", "below", None, "7 days",
        "filter(count(*), WHERE state = 'ready') / count(*)",
        _ASSISTANT_POLLS,
        "count(*)",
    ),
    "publication-outbox-age": _nrql(
        "publication", "baseline_required", "above", None, "24 hours",
        "latest(outbox_oldest_age_seconds)", _OUTBOX_GAUGE, "count(*)",
    ),
    "publication-to-match-lag": _nrql(
        "match_recompute", "baseline_required", "above", None, "14 days",
        "max(publication_lag_max_seconds)", _RECOMPUTE_LAG, "count(*)",
    ),
    "matching-alerts-quiet": _quiet(
        "match_recompute", "7 days", "matching_batch", "stage = 'match_recompute'",
        ["matching-backlog", "deadline-resource-pressure"],
    ),
    "evidence-stale-share": _nrql(
        "organization_crawl", "baseline_required", "above", None, "24 hours",
        "latest(evidence_stale_rows) / latest(accepted_evidence_rows)",
        "event_name = 'pipeline_health'",
        "latest(accepted_evidence_rows)",
    ),
}

#: Which direction is bad for each thing a gate can measure.
BAD_WHEN = {
    "priorities-apply-success": "below",      # a success share
    "interactive-reply-success": "below",     # a success share
    "assistant-ready-share": "below",         # a good-state share
    "interactive-time-to-first-result": "above",  # a duration
    "job-matches-source-unavailable": "above",    # a bad-state share
    "publication-outbox-age": "above",        # an age
    "publication-to-match-lag": "above",      # a lag
    "evidence-stale-share": "above",          # a stale share
    "interactive-alerts-quiet": "above",      # open alerts
    "job-source-alerts-quiet": "above",
    "matching-alerts-quiet": "above",
}

#: The phase whose capability first makes an availability state reachable, for
#: every state a gate counts in its numerator. ``AssistantStateByPhaseTests``
#: proves the ``ready`` entry against the view.
STATE_PHASE = {
    ("assistant_status", "ready"): "job_source",
    ("job_matches", "no_source"): "job_source",
    ("job_matches", "source_disabled"): "job_source",
}

#: The evaluation procedure of docs/monitoring.md: ``(check, outcome)`` in
#: order. The first check that is true decides.
RULES = (
    ("`min_sample` is `null`", "hold"),
    ("the window is not clean", "hold"),
    ("the sample is missing or below `min_sample`", "hold"),
    ("`kind: alerts_quiet` and `policy_confirmed` is not `true`", "hold"),
    ("`threshold` is `null`, or the query returned no value", "hold"),
    (
        "the value is `operator` the `threshold` (`above`: value > threshold;"
        " `below`: value < threshold)",
        "breach",
    ),
    ("none of the above", "pass"),
)
FLOORS_HEADING = "### Sample floors and alert policies to lock"
RELEASE_SECTION = "Contextual assistant staged release (#492)"

_ENUM_EQUALS = re.compile(r"\b([a-z_]+)\s*(?:!=|=)\s*'([^']*)'")
_ENUM_IN = re.compile(r"\b([a-z_]+)\s+(?:NOT\s+)?IN\s*\(([^)]*)\)", re.IGNORECASE)
_CITATION = re.compile(r"`([^`\s:]+)::([^`]+)`")


def _load_yaml() -> dict:
    return yaml.safe_load(MONITORING_YAML.read_text(encoding="utf-8"))


def _gates() -> list:
    return _load_yaml()["release_gates"]


def _slug(heading: str) -> str:
    """GitHub-style anchor for a Markdown heading line."""
    text = heading.lstrip("#").strip().lower()
    return re.sub(r"[^a-z0-9 _-]", "", text).replace(" ", "-")


def _headings(path: Path) -> list:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    ]


def _section(path: Path, title: str) -> str:
    """Text of the ``##`` section with this title, up to the next ``##``/``#``."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"## {title}")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith(("## ", "# "))
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _table_rows(text: str) -> list:
    """Cells of every Markdown table body row in ``text``."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or set(line) <= set("|- "):
            continue
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def _table_after(text: str, marker: str) -> list:
    """Body rows of the first Markdown table that follows ``marker``."""
    lines = text.split(marker, 1)[1].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("|"))
    end = next(
        (i for i in range(start, len(lines)) if not lines[i].startswith("|")),
        len(lines),
    )
    return _table_rows("\n".join(lines[start:end]))[1:]


def _split_query(nrql: str) -> tuple:
    """``(select expression, text from FROM onward)`` of a gate query."""
    select, tail = nrql.split(FROM_CLAUSE, 1)
    assert select.startswith("SELECT "), nrql
    return select[len("SELECT "):], tail


def _is_ratio(gate: dict) -> bool:
    return gate["kind"] == "nrql" and " / " in _split_query(gate["nrql"])[0]


def _checks(gate: dict, sample, value, window_clean: bool) -> tuple:
    """Truth of each ``RULES`` check for one observation, in the same order."""
    floor, threshold = gate["min_sample"], gate["threshold"]
    no_number = threshold is None or value is None
    if no_number:
        breached = False
    elif gate["operator"] == "above":
        breached = value > threshold
    else:
        breached = value < threshold
    return (
        floor is None,
        not window_clean,
        floor is not None and (sample is None or sample < floor),
        gate["kind"] == "alerts_quiet" and gate["policy_confirmed"] is not True,
        no_number,
        breached,
        True,
    )


def _decided_at(gate: dict, sample, value, window_clean: bool = True) -> int:
    """1-based number of the first procedure step whose check is true."""
    checks = _checks(gate, sample, value, window_clean)
    assert len(checks) == len(RULES)
    return checks.index(True) + 1


def _outcome(gate: dict, sample, value, window_clean: bool = True) -> str:
    """The documented evaluation procedure (docs/monitoring.md), in order."""
    return RULES[_decided_at(gate, sample, value, window_clean) - 1][1]


def _phase_decision(outcomes) -> str:
    """How a phase combines its gates (docs/monitoring.md, "Phase decision")."""
    outcomes = list(outcomes)
    if "breach" in outcomes:
        return "do not expand"
    if "hold" in outcomes or not outcomes:
        return "hold"
    return "may expand"


def _matches(where: str, attributes: dict) -> bool:
    """Evaluate the small NRQL ``WHERE`` subset the gate filters use."""
    for clause in where.split(" AND "):
        key, _, rest = clause.partition(" ")
        if rest == "IS NOT NULL":
            ok = attributes.get(key) is not None
        elif rest.startswith("= '"):
            ok = attributes.get(key) == rest[3:-1]
        elif rest.startswith("!= '"):
            ok = attributes.get(key) != rest[4:-1]
        elif rest.startswith("> "):
            ok = (attributes.get(key) or 0) > float(rest[2:])
        else:
            raise AssertionError(f"unsupported clause: {clause}")
        if not ok:
            return False
    return True


def _observations(gate: dict, largest_sample: int = 6):
    """Every ``(sample, value)`` a gate's own queries could return.

    The value is tied to the sample the way the queries tie them: a ratio is
    ``k / sample`` for ``0 <= k <= sample``; a gate whose query *is* its sample
    query returns the sample itself; anything else (an aggregate, or the
    number of named alerts opened) is free once at least one event exists.
    """
    for sample in range(largest_sample + 1):
        if gate["kind"] == "alerts_quiet":
            values = (0, 1, 5)
        elif sample == 0:
            values = (None,)
        elif _is_ratio(gate):
            values = tuple(k / sample for k in range(sample + 1))
        elif gate["nrql"] == gate["sample_nrql"]:
            values = (sample,)
        else:
            values = (0, 1, 10**6)
        for value in values:
            yield sample, value


def _outcomes(gate: dict) -> set:
    return {_outcome(gate, sample, value) for sample, value in _observations(gate)}


def _locked(gate: dict, **overrides) -> dict:
    """A copy of a gate as a threshold-lock pull request would leave it."""
    locked = dict(gate, min_sample=3)
    if locked["threshold"] is None:
        locked["threshold"] = 0.5 if _is_ratio(gate) else 1
    if locked["kind"] == "alerts_quiet":
        locked["policy_confirmed"] = True
    locked.update(overrides)
    return locked


def _nrql_enum_literals(nrql: str) -> list:
    """Every ``(key, literal)`` compared in a query, for ``=``, ``!=`` and ``IN``."""
    pairs = [(key, value) for key, value in _ENUM_EQUALS.findall(nrql)]
    for key, values in _ENUM_IN.findall(nrql):
        pairs.extend((key, value) for value in re.findall(r"'([^']*)'", values))
    return pairs


class ReleaseGateShapeTests(SimpleTestCase):
    """Every gate is well formed and no threshold is invented."""

    def test_gate_names_are_the_reserved_set_and_unique(self):
        names = [gate["name"] for gate in _gates()]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(GATE_NAMES))

    def test_common_fields_present_and_enumerated(self):
        for gate in _gates():
            with self.subTest(gate=gate["name"]):
                self.assertTrue(COMMON_KEYS <= set(gate))
                self.assertIn(gate["phase"], PHASE_IDS)
                self.assertIn(gate["kind"], ("nrql", "alerts_quiet"))
                self.assertIn(gate["status"], ("provisional", "baseline_required"))
                for key in ("window", "on_breach", "runbook"):
                    self.assertTrue(str(gate[key]).strip(), key)

    def test_switch_is_a_registered_key_or_null_only_for_the_shell(self):
        for gate in _gates():
            with self.subTest(gate=gate["name"]):
                if gate["switch"] is None:
                    self.assertEqual(gate["phase"], "shell")
                else:
                    self.assertIn(gate["switch"], ALLOWED_CAPABILITY_KEYS)
                    self.assertNotEqual(gate["phase"], "shell")

    def test_each_phase_uses_one_switch(self):
        """No two phases share a switch and no phase spans two switches."""
        by_phase = {}
        for gate in _gates():
            by_phase.setdefault(gate["phase"], set()).add(gate["switch"])
        for phase, switches in by_phase.items():
            self.assertEqual(len(switches), 1, phase)
        switches = [next(iter(value)) for value in by_phase.values()]
        self.assertEqual(len(switches), len(set(switches)))

    def test_every_gate_measures_what_is_pinned(self):
        """Phase, switch, operator, threshold, queries and alert list."""
        gates = {gate["name"]: gate for gate in _gates()}
        self.assertEqual(list(gates), list(GATE_NAMES))
        self.assertEqual(set(PINNED_GATES), set(GATE_NAMES))
        for name, pinned in PINNED_GATES.items():
            gate = gates[name]
            with self.subTest(gate=name):
                for key in (
                    "phase", "switch", "kind", "status", "operator", "threshold",
                    "window",
                ):
                    self.assertEqual(gate[key], pinned[key], key)
                since = f" SINCE {pinned['window']} ago"
                if pinned["kind"] == "nrql":
                    tail = f"{FROM_CLAUSE}WHERE {pinned['where']}{since}"
                    self.assertEqual(gate["nrql"], f"SELECT {pinned['select']}{tail}")
                    self.assertEqual(
                        gate["sample_nrql"], f"SELECT {pinned['sample_select']}{tail}"
                    )
                else:
                    self.assertEqual(gate["alerts"], pinned["alerts"])
                    self.assertEqual(gate["signal_event"], pinned["signal_event"])
                    self.assertEqual(gate["signal_filter"], pinned["signal_filter"])
                    self.assertEqual(
                        gate["sample_nrql"],
                        f"SELECT count(*){FROM_CLAUSE}WHERE event_name ="
                        f" '{pinned['signal_event']}' AND"
                        f" {pinned['signal_filter']}{since}",
                    )

    def test_operator_points_at_the_bad_direction(self):
        """A success share breaches below; an age, lag or bad share above."""
        self.assertEqual(set(BAD_WHEN), set(GATE_NAMES))
        for gate in _gates():
            with self.subTest(gate=gate["name"]):
                self.assertEqual(gate["operator"], BAD_WHEN[gate["name"]])

    def test_reply_success_ratio_cannot_exceed_one(self):
        """Replied over attempted, not the inverse: the numerator is a subset."""
        gates = {gate["name"]: gate for gate in _gates()}
        select = _split_query(gates["interactive-reply-success"]["nrql"])[0]
        numerator, denominator = select.split(" / ")
        self.assertIn("'replied'", numerator)
        self.assertIn("'attempted'", denominator)

    def test_job_source_gate_leaves_out_the_six_hour_success_alert(self):
        """``no-recent-success`` opens routinely at a six-hour cadence."""
        doc = _load_yaml()
        alerts = {alert["name"]: alert["nrql"] for alert in doc["alerts"]}
        self.assertIn("SINCE 6 hours ago", alerts["no-recent-success"])
        pipeline = (REPO_ROOT / "deploy" / "cronjob-job-pipeline.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn('schedule: "0 */6 * * *"', pipeline)
        for gate in _gates():
            self.assertNotIn("no-recent-success", gate.get("alerts", []))

    def test_nrql_gates_carry_query_fields(self):
        gates = [gate for gate in _gates() if gate["kind"] == "nrql"]
        self.assertTrue(gates)
        for gate in gates:
            with self.subTest(gate=gate["name"]):
                self.assertTrue(NRQL_KEYS <= set(gate))
                self.assertIn(gate["event"], monitoring.EVENT_NAMES)
                self.assertIn("FROM CrankOperation", gate["nrql"])
                self.assertIn(f"event_name = '{gate['event']}'", gate["nrql"])
                self.assertNotIn("signal_event", gate)
                self.assertNotIn("policy_confirmed", gate)

    def test_every_gate_declares_operator_threshold_and_sample(self):
        for gate in _gates():
            with self.subTest(gate=gate["name"]):
                self.assertIn(gate["operator"], ("above", "below"))
                self.assertIn(type(gate["threshold"]), (int, float, type(None)))
                self.assertIn(FROM_CLAUSE, gate["sample_nrql"])

    def test_no_sample_floor_is_set_before_a_gate_is_locked(self):
        """A floor of 1 lets one lucky event pass, so none is invented here.

        ``provisional`` and ``baseline_required`` are the only statuses, and
        both require ``min_sample: null``. The owner's threshold-lock pull
        request sets the floor and the status together and updates this test.
        """
        for gate in _gates():
            with self.subTest(gate=gate["name"]):
                self.assertIn(gate["status"], ("provisional", "baseline_required"))
                self.assertIsNone(gate["min_sample"])

    def test_window_matches_the_since_clause_of_every_query(self):
        for gate in _gates():
            queries = [gate["sample_nrql"]]
            if gate["kind"] == "nrql":
                queries.append(gate["nrql"])
            for query in queries:
                with self.subTest(gate=gate["name"], query=query):
                    self.assertRegex(gate["window"], r"^[1-9][0-9]* (hours|days)$")
                    self.assertEqual(query.count("SINCE"), 1)
                    self.assertTrue(query.endswith(f" SINCE {gate['window']} ago"))

    def test_sample_query_counts_what_the_gate_reads(self):
        """Ratio: the denominator. Otherwise: the events behind the value."""
        for gate in _gates():
            if gate["kind"] != "nrql":
                continue
            with self.subTest(gate=gate["name"]):
                select, tail = _split_query(gate["nrql"])
                sample_select, sample_tail = _split_query(gate["sample_nrql"])
                self.assertEqual(sample_tail, tail)
                if _is_ratio(gate):
                    self.assertEqual(sample_select, select.split(" / ", 1)[1])
                else:
                    self.assertEqual(sample_select, "count(*)")
                self.assertNotEqual(gate["sample_nrql"], gate["nrql"])

    def test_alerts_quiet_gates_name_existing_alerts(self):
        alert_names = {alert["name"] for alert in _load_yaml()["alerts"]}
        gates = [gate for gate in _gates() if gate["kind"] == "alerts_quiet"]
        self.assertTrue(gates)
        for gate in gates:
            with self.subTest(gate=gate["name"]):
                self.assertTrue(gate["alerts"])
                self.assertEqual(len(gate["alerts"]), len(set(gate["alerts"])))
                self.assertTrue(set(gate["alerts"]) <= alert_names)
                self.assertNotIn("nrql", gate)
                self.assertNotIn("event", gate)
                self.assertTrue(QUIET_KEYS <= set(gate))
                self.assertEqual((gate["operator"], gate["threshold"]), ("above", 0))

    def test_alerts_quiet_gates_require_a_live_signal(self):
        """"No alert opened" is also true when nothing emits.

        Each gate names the event its alerts read, counts it over the gate's
        window as the sample — narrowed by ``signal_filter`` to events this
        phase emits with a measurement — and stays unconfirmed until the
        owner checks the alert policy exists (the repository wires none).
        """
        alerts = {alert["name"]: alert["nrql"] for alert in _load_yaml()["alerts"]}
        for gate in _gates():
            if gate["kind"] != "alerts_quiet":
                continue
            with self.subTest(gate=gate["name"]):
                signal = gate["signal_event"]
                self.assertIn(signal, monitoring.EVENT_NAMES)
                reading = [
                    name
                    for name in gate["alerts"]
                    if f"event_name = '{signal}'" in alerts[name]
                ]
                self.assertTrue(reading, signal)
                self.assertTrue(gate["signal_filter"].strip())
                self.assertEqual(
                    gate["sample_nrql"],
                    f"SELECT count(*){FROM_CLAUSE}WHERE event_name = '{signal}'"
                    f" AND {gate['signal_filter']} SINCE {gate['window']} ago",
                )
                self.assertIs(gate["policy_confirmed"], False)

    def test_job_source_signal_is_the_healthcheck_event(self):
        gates = {gate["name"]: gate for gate in _gates()}
        self.assertEqual(
            gates["job-source-alerts-quiet"]["signal_event"], "inventory_health"
        )

    def test_provisional_gates_have_a_number_and_a_resolvable_source(self):
        slugs = [_slug(line) for line in _headings(ROLLOUT_DOC)]
        gates = [gate for gate in _gates() if gate["status"] == "provisional"]
        self.assertTrue(gates)
        for gate in gates:
            with self.subTest(gate=gate["name"]):
                self.assertIn(type(gate["threshold"]), (int, float))
                path, _, anchor = gate["source"].partition("#")
                self.assertEqual(path, "docs/rollout-gates.md")
                self.assertEqual(slugs.count(anchor), 1, anchor)

    def test_baseline_required_gates_have_no_threshold(self):
        gates = [gate for gate in _gates() if gate["status"] == "baseline_required"]
        self.assertTrue(gates)
        for gate in gates:
            with self.subTest(gate=gate["name"]):
                self.assertIsNone(gate["threshold"])
                self.assertNotIn("source", gate)

    def test_runbooks_resolve_to_one_heading(self):
        for gate in _gates():
            with self.subTest(gate=gate["name"]):
                path, _, anchor = gate["runbook"].partition("#")
                slugs = [_slug(line) for line in _headings(REPO_ROOT / path)]
                self.assertEqual(slugs.count(anchor), 1, anchor)

    def test_gates_add_no_alert_and_keep_metrics_baseline_only(self):
        """#540's invariant: gates are evidence queries, not alerts."""
        doc = _load_yaml()
        self.assertEqual(doc["version"], 1)
        self.assertTrue(all(m["baseline_only"] is True for m in doc["metrics"]))
        self.assertEqual(len(doc["alerts"]), 12)
        alert_names = {alert["name"] for alert in doc["alerts"]}
        self.assertFalse(alert_names & set(GATE_NAMES))


class ReleaseGateEvaluationTests(SimpleTestCase):
    """What the documented rules decide for every result a query can return."""

    def test_no_gate_can_pass_as_checked_in(self):
        for gate in _gates():
            with self.subTest(gate=gate["name"]):
                self.assertEqual(_outcomes(gate), {"hold"})

    def test_no_gate_can_pass_while_its_floor_is_unset(self):
        """Even with a number and a confirmed policy, no floor means hold."""
        for gate in _gates():
            with self.subTest(gate=gate["name"]):
                self.assertEqual(_outcomes(_locked(gate, min_sample=None)), {"hold"})

    def test_one_lucky_event_cannot_pass_below_the_floor(self):
        gates = {gate["name"]: gate for gate in _gates()}
        gate = _locked(gates["interactive-reply-success"])
        self.assertEqual(_outcome(gate, 1, 1.0), "hold")
        self.assertEqual(_outcome(gate, 2, 1.0), "hold")
        self.assertEqual(_outcome(gate, 3, 1.0), "pass")
        self.assertEqual(_outcome(gate, 3, 2 / 3), "breach")
        # The floor the first version of this gate carried is why it is unset.
        self.assertEqual(_outcome(_locked(gate, min_sample=1), 1, 1.0), "pass")

    def test_every_locked_gate_can_pass_hold_and_breach(self):
        for gate in _gates():
            with self.subTest(gate=gate["name"]):
                self.assertEqual(
                    _outcomes(_locked(gate)), {"pass", "hold", "breach"}
                )

    def test_source_unavailable_gate_is_a_share_that_can_pass(self):
        gates = {gate["name"]: gate for gate in _gates()}
        gate = gates["job-matches-source-unavailable"]
        self.assertTrue(_is_ratio(gate))
        self.assertIn(
            "filter(count(*), WHERE state IN ('no_source', 'source_disabled'))"
            " / count(*)",
            gate["nrql"],
        )
        self.assertIn("surface = 'job_matches'", gate["sample_nrql"])
        self.assertNotIn("no_source", gate["sample_nrql"])
        locked = _locked(gate)
        self.assertEqual(_outcome(locked, 0, None), "hold")
        self.assertEqual(_outcome(locked, 5, 0.0), "pass")
        self.assertEqual(_outcome(locked, 5, 1 / 5), "breach")

    def test_a_count_of_only_bad_events_could_never_pass(self):
        """The shape this gate first had: the value was its own sample."""
        query = (
            "SELECT count(*) FROM CrankOperation WHERE event_name ="
            " 'availability_state' AND surface = 'job_matches' AND state IN"
            " ('no_source', 'source_disabled') SINCE 7 days ago"
        )
        gate = {
            "kind": "nrql", "nrql": query, "sample_nrql": query,
            "operator": "above", "threshold": 0, "min_sample": 1,
        }
        self.assertEqual(_outcomes(gate), {"hold", "breach"})

    def test_alerts_quiet_holds_without_signal_or_confirmed_policy(self):
        for gate in _gates():
            if gate["kind"] != "alerts_quiet":
                continue
            with self.subTest(gate=gate["name"]):
                locked = _locked(gate)
                # Nothing emitted: quiet, but not evidence.
                self.assertEqual(_outcome(locked, 0, 0), "hold")
                self.assertEqual(_outcome(locked, 2, 0), "hold")
                self.assertEqual(_outcome(locked, 3, 0), "pass")
                self.assertEqual(_outcome(locked, 3, 1), "breach")
                unconfirmed = _locked(gate, policy_confirmed=False)
                self.assertEqual(_outcomes(unconfirmed), {"hold"})

    def test_missing_value_or_threshold_holds(self):
        gates = {gate["name"]: gate for gate in _gates()}
        gate = _locked(gates["publication-outbox-age"])
        self.assertEqual(_outcome(gate, 5, None), "hold")
        self.assertEqual(_outcome(gate, None, 0), "hold")
        self.assertEqual(_outcome(gate, 5, 0), "pass")
        self.assertEqual(_outcome(gate, 5, 2), "breach")
        self.assertEqual(_outcome(_locked(gate, threshold=None), 5, 0), "hold")

    def test_window_that_is_not_clean_holds(self):
        """A result that includes time before enablement decides nothing."""
        for gate in _gates():
            with self.subTest(gate=gate["name"]):
                locked = _locked(gate)
                outcomes = {
                    _outcome(locked, sample, value, window_clean=False)
                    for sample, value in _observations(locked)
                }
                self.assertEqual(outcomes, {"hold"})

    def test_a_value_equal_to_the_threshold_passes(self):
        gates = {gate["name"]: gate for gate in _gates()}
        self.assertEqual(
            _outcome(_locked(gates["interactive-reply-success"]), 10, 0.90), "pass"
        )
        self.assertEqual(
            _outcome(_locked(gates["job-matches-source-unavailable"]), 10, 0), "pass"
        )

    def test_a_held_gate_blocks_expansion(self):
        self.assertEqual(_phase_decision(["pass", "pass"]), "may expand")
        self.assertEqual(_phase_decision(["pass", "hold"]), "hold")
        self.assertEqual(_phase_decision(["hold", "breach"]), "do not expand")
        self.assertEqual(_phase_decision(["pass", "breach"]), "do not expand")
        self.assertEqual(_phase_decision([]), "hold")
        # As checked in, every phase that has gates is on hold.
        for phase in PHASE_IDS:
            gates = [gate for gate in _gates() if gate["phase"] == phase]
            outcomes = [_outcome(gate, 10**6, 0) for gate in gates]
            self.assertEqual(_phase_decision(outcomes), "hold", phase)


class EvaluationProcedureDocTests(SimpleTestCase):
    """docs/monitoring.md states the procedure ``_outcome`` applies."""

    def setUp(self):
        self.text = MONITORING_DOC.read_text(encoding="utf-8")
        self.normalized = " ".join(self.text.split())
        self.gates = {gate["name"]: gate for gate in _gates()}

    def test_procedure_table_is_the_rules_in_order(self):
        rows = _table_after(self.text, "**Evaluation procedure.**")
        self.assertEqual(
            [(row[1], row[2]) for row in rows], [tuple(rule) for rule in RULES]
        )
        self.assertEqual(
            [row[0] for row in rows], [str(n) for n in range(1, len(RULES) + 1)]
        )
        self.assertIn(
            "The first step whose check is true decides the outcome; later steps"
            " are not read.",
            self.normalized,
        )
        self.assertIn(
            "A value equal to the threshold is a pass under either operator.",
            self.normalized,
        )

    def test_yaml_comment_lists_the_same_steps(self):
        comment = " ".join(
            line.lstrip("# ")
            for line in MONITORING_YAML.read_text(encoding="utf-8").splitlines()
            if line.startswith("#")
        )
        outcomes = re.findall(r"\b([1-7])\. .*?-> (hold|breach|pass)", comment)
        self.assertEqual(
            outcomes,
            [(str(n), rule[1]) for n, rule in enumerate(RULES, start=1)],
        )

    def test_doc_defines_sample_value_window_and_phase_decision(self):
        for phrase in (
            "#### Release decision gates",
            "the denominator of the ratio",
            "the number of `signal_event` events in the window that match"
            " `signal_filter`",
            "**open at any time in the window**",
            "An alert that was already open when the window started counts.",
            "read from the alerting tool's incident history",
            "**A clean window.**",
            "at least `window` has passed since the phase's post-merge check"
            " succeeded",
            "the phase's switch was not turned off and no job source was disabled",
            "Any gate is **breach** — do not expand.",
            "any gate is **hold** — the decision is **hold**.",
            "A held gate blocks expansion; it is never read as a pass.",
            "No gate can pass as checked in",
            "They are not defaults and are not proposed values.",
        ):
            self.assertIn(phrase, self.normalized)

    def test_signal_filter_table_matches_the_yaml(self):
        rows = _table_after(self.text, "The three signal filters, and why each exists:")
        documented = {row[0].strip("`"): row[1].strip("`") for row in rows}
        expected = {
            gate["name"]: gate["signal_filter"]
            for gate in _gates()
            if gate["kind"] == "alerts_quiet"
        }
        self.assertEqual(documented, expected)
        for row in rows:
            self.assertTrue(row[2], row[0])

    def _step(self, cell: str) -> int:
        return int(re.match(r"step ([1-7])", cell).group(1))

    def test_ratio_worked_example_follows_the_procedure(self):
        gate = dict(self.gates["interactive-reply-success"], min_sample=200)
        self.assertIn("supposing the owner had locked `min_sample: 200`", self.normalized)
        rows = _table_after(self.text, "*Ratio gate* — `interactive-reply-success`")
        self.assertEqual(len(rows), 4)
        for sample, value, decided, outcome in rows:
            with self.subTest(sample=sample, value=value):
                self.assertEqual(
                    _decided_at(gate, int(sample), float(value)), self._step(decided)
                )
                self.assertEqual(_outcome(gate, int(sample), float(value)), outcome)
                # As checked in, the same numbers decide nothing.
                self.assertEqual(
                    _decided_at(
                        self.gates["interactive-reply-success"],
                        int(sample),
                        float(value),
                    ),
                    1,
                )
        self.assertEqual({row[3] for row in rows}, {"hold", "breach", "pass"})

    def test_aggregate_worked_example_follows_the_procedure(self):
        base = dict(self.gates["publication-outbox-age"], min_sample=20)
        self.assertIn("supposing `min_sample: 20` and a clean window", self.normalized)
        rows = _table_after(self.text, "*Aggregate gate* — `publication-outbox-age`")
        self.assertEqual(len(rows), 4)
        for sample, value, threshold, decided, outcome in rows:
            with self.subTest(sample=sample, value=value, threshold=threshold):
                gate = dict(
                    base,
                    threshold=None if threshold.startswith("`null`") else int(threshold),
                )
                observed = None if value == "no value" else int(value)
                self.assertEqual(
                    _decided_at(gate, int(sample), observed), self._step(decided)
                )
                self.assertEqual(_outcome(gate, int(sample), observed), outcome)
        self.assertEqual({row[4] for row in rows}, {"hold", "breach", "pass"})

    def test_alerts_quiet_worked_example_follows_the_procedure(self):
        base = dict(self.gates["matching-alerts-quiet"], min_sample=500)
        self.assertIn("supposing `min_sample: 500` and a clean window", self.normalized)
        rows = _table_after(self.text, "*Alerts-quiet gate* — `matching-alerts-quiet`")
        self.assertEqual(len(rows), 5)
        for sample, opened, confirmed, decided, outcome in rows:
            with self.subTest(sample=sample, opened=opened, confirmed=confirmed):
                gate = dict(base, policy_confirmed=confirmed.startswith("`true`"))
                sample = int(sample.split(" ")[0])
                opened = int(opened.split(" ")[0])
                self.assertEqual(_decided_at(gate, sample, opened), self._step(decided))
                self.assertEqual(_outcome(gate, sample, opened), outcome)
        self.assertEqual({row[4] for row in rows}, {"hold", "breach", "pass"})
        # 288 five-minute slots a day for seven days.
        self.assertEqual(288 * 7, 2016)
        recompute = (REPO_ROOT / "deploy" / "cronjob-match-recompute.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn('schedule: "*/5 * * * *"', recompute)

    def test_phase_worked_example(self):
        self.assertIn(
            "`match_recompute` with `publication-to-match-lag` on hold and"
            " `matching-alerts-quiet` passing: the phase decision is **hold**.",
            self.normalized,
        )
        self.assertEqual(_phase_decision(["hold", "pass"]), "hold")


class ReleaseGateTelemetryTests(TestCase):
    """Gate queries only compare enum keys against registered values."""

    def test_nrql_enum_literals_are_registered(self):
        registry = monitoring.enum_values()
        checked = 0
        for gate in _gates():
            if gate["kind"] != "nrql":
                continue
            for key, value in _nrql_enum_literals(gate["nrql"]):
                if key not in monitoring._ENUM_KEYS:
                    continue
                with self.subTest(gate=gate["name"], key=key, value=value):
                    self.assertIn(value, registry[key])
                    checked += 1
        self.assertGreater(checked, 0)

    def test_sample_queries_compare_registered_enum_values(self):
        registry = monitoring.enum_values()
        checked = 0
        for gate in _gates():
            for key, value in _nrql_enum_literals(gate["sample_nrql"]):
                if key not in monitoring._ENUM_KEYS:
                    continue
                with self.subTest(gate=gate["name"], key=key, value=value):
                    self.assertIn(value, registry[key])
                    checked += 1
        self.assertGreater(checked, 0)

    def test_signal_filters_reject_other_phases_and_error_events(self):
        """The floor of a quiet gate cannot be met by events that prove nothing.

        Each payload is passed through the telemetry sanitizer, so the filter
        is evaluated on what New Relic would actually receive.
        """
        gates = {gate["name"]: gate for gate in _gates()}
        cases = {
            "interactive-alerts-quiet": (
                [{"status": "provider_succeeded", "estimated_cost_usd": 0.01}],
                [
                    {"status": "error", "reason_code": "rate_limited"},
                    {"status": "provider_failed", "reason_code": "timeout"},
                    {"status": "failed", "reason_code": "timeout"},
                ],
            ),
            "job-source-alerts-quiet": (
                [
                    {"healthy": True, "enabled_sources": 2, "active_listings": 9},
                    # An unhealthy inventory is still a measurement.
                    {"healthy": False, "enabled_sources": 0, "active_listings": 0},
                ],
                [{"healthy": False, "reason_code": "database"}],
            ),
            "matching-alerts-quiet": (
                [{"stage": "match_recompute", "users_failed": 0}],
                [{"stage": "job_pipeline_matching", "users_failed": 0}],
            ),
        }
        self.assertEqual(
            set(cases), {g["name"] for g in _gates() if g["kind"] == "alerts_quiet"}
        )
        for name, (counted, ignored) in cases.items():
            gate = gates[name]
            where = f"event_name = '{gate['signal_event']}' AND {gate['signal_filter']}"
            for payload in counted:
                with self.subTest(gate=name, counted=payload):
                    attributes = monitoring.event_attributes(
                        gate["signal_event"], payload
                    )
                    self.assertTrue(_matches(where, attributes))
            for payload in ignored:
                with self.subTest(gate=name, ignored=payload):
                    attributes = monitoring.event_attributes(
                        gate["signal_event"], payload
                    )
                    self.assertFalse(_matches(where, attributes))

    def test_matching_gates_read_only_the_recompute_stage(self):
        """``matching_batch`` is also emitted by the job pipeline's phase."""
        gates = {gate["name"]: gate for gate in _gates()}
        pipeline = monitoring.event_attributes(
            "matching_batch",
            {
                "stage": "job_pipeline_matching",
                "publication_lag_count": 3,
                "publication_lag_max_seconds": 90,
            },
        )
        recompute = dict(pipeline, stage="match_recompute")
        unmeasured = dict(recompute, publication_lag_count=0)
        for name in ("publication-to-match-lag", "matching-alerts-quiet"):
            for query in (gates[name].get("nrql"), gates[name]["sample_nrql"]):
                if query is None:
                    continue
                where = query.split(" WHERE ", 1)[1].split(" SINCE ")[0]
                with self.subTest(gate=name, where=where):
                    self.assertFalse(_matches(where, pipeline))
                    self.assertTrue(_matches(where, recompute))
        lag = gates["publication-to-match-lag"]["nrql"]
        where = lag.split(" WHERE ", 1)[1].split(" SINCE ")[0]
        self.assertFalse(_matches(where, unmeasured))

    def test_degraded_health_events_do_not_count_as_outbox_samples(self):
        gate = {g["name"]: g for g in _gates()}["publication-outbox-age"]
        where = gate["sample_nrql"].split(" WHERE ", 1)[1].split(" SINCE ")[0]
        healthy = monitoring.event_attributes(
            "pipeline_health", {"healthy": True, "outbox_oldest_age_seconds": 0}
        )
        degraded = monitoring.event_attributes(
            "pipeline_health", {"healthy": False, "reason_code": "database"}
        )
        self.assertTrue(_matches(where, healthy))
        self.assertFalse(_matches(where, degraded))

    def test_filter_evaluator_rejects_clauses_it_does_not_know(self):
        with self.assertRaises(AssertionError):
            _matches("state LIKE 'x%'", {})

    def test_literal_parser_reads_equals_not_equals_and_in(self):
        self.assertEqual(
            _nrql_enum_literals(
                "WHERE event_name = 'x' AND state != 'a' AND state IN ('b', 'c')"
            ),
            [("event_name", "x"), ("state", "a"), ("state", "b"), ("state", "c")],
        )

    def test_queries_survive_the_known_telemetry_gaps(self):
        """Two queries are written to stay correct across #543."""
        gates = {gate["name"]: gate for gate in _gates()}
        self.assertIn(
            "publication_lag_count > 0", gates["publication-to-match-lag"]["nrql"]
        )
        self.assertIn(
            "state != 'signed_out'", gates["assistant-ready-share"]["nrql"]
        )


class GateStatesBelongToTheirPhaseTests(TestCase):
    """A gate may only count a state its own phase can produce."""

    READY = {
        "INTERACTIVE_AGENT_ENABLED": True,
        "JOB_SEARCH_PROVIDER": "orchestrator",
        "ENV": "prod",
    }

    def _numerator_states(self, gate: dict) -> list:
        select = _split_query(gate["nrql"])[0]
        numerator = select.split(" / ")[0]
        return [value for key, value in _nrql_enum_literals(numerator) if key == "state"]

    def test_every_state_a_gate_counts_is_mapped_to_that_gate_s_phase(self):
        checked = 0
        for gate in _gates():
            if gate.get("event") != "availability_state":
                continue
            surface = dict(_nrql_enum_literals(gate["nrql"]))["surface"]
            states = self._numerator_states(gate)
            self.assertTrue(states, gate["name"])
            for state in states:
                with self.subTest(gate=gate["name"], state=state):
                    self.assertEqual(STATE_PHASE[(surface, state)], gate["phase"])
                    checked += 1
        self.assertEqual(checked, len(STATE_PHASE))

    def _state(self) -> str:
        with mock.patch.object(assistant_status, "_build_provider"):
            return assistant_status._classify_authenticated_state()

    def _add_listing(self, *, enabled: bool = True) -> JobSourceCatalog:
        source = JobSourceCatalog.objects.create(
            name="Gate Phase Source",
            adapter_key="test-adapter",
            base_url="https://data.usajobs.gov/api/search",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=enabled,
        )
        now = timezone.now()
        JobListing.objects.create(
            source=source,
            canonical_url="https://data.usajobs.gov/listings/492",
            employer_name="Example Corp",
            title="Backend Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
        )
        return source

    def test_ready_is_unreachable_until_the_job_source_phase(self):
        """Replies on, no inventory: every signed-in poll is not ``ready``."""
        with override_settings(**self.READY):
            # interactive_replies enabled, job_source not yet.
            self.assertEqual(self._state(), "inventory_unavailable")
            # A source that exists but is disabled is still not inventory.
            source = self._add_listing(enabled=False)
            self.assertEqual(self._state(), "inventory_unavailable")
            # job_source enabled: an active listing under an enabled source.
            JobSourceCatalog.objects.filter(pk=source.pk).update(enabled=True)
            self.assertEqual(self._state(), "ready")
            # A pipeline run in flight is job_source's doing as well.
            AgentRun.objects.create(
                run_type=AgentRun.RunType.JOB_PIPELINE, status=AgentRun.Status.RUNNING
            )
            self.assertEqual(self._state(), "refreshing")

    def test_ready_also_needs_the_earlier_phase_to_stay_on(self):
        self._add_listing()
        with override_settings(**dict(self.READY, INTERACTIVE_AGENT_ENABLED=False)):
            self.assertEqual(self._state(), "replies_disabled")

    def test_rollout_doc_explains_where_the_gate_sits(self):
        section = " ".join(_section(ROLLOUT_DOC, RELEASE_SECTION).split())
        self.assertIn(
            "`assistant-ready-share` belongs to `job_source`, not to"
            " `interactive_replies`",
            section,
        )
        self.assertIn("every signed-in poll is `inventory_unavailable`", section)


class RolloutDocReleaseSectionTests(SimpleTestCase):
    """The #492 section of docs/rollout-gates.md matches the gate data."""

    def setUp(self):
        self.section = _section(ROLLOUT_DOC, RELEASE_SECTION)

    def test_section_sits_between_registry_and_evidence_storage(self):
        titles = [
            line[3:] for line in _headings(ROLLOUT_DOC) if line.startswith("## ")
        ]
        index = titles.index(RELEASE_SECTION)
        self.assertEqual(
            titles[index - 1], "Capability Registry (Epic #454 — issue #463)"
        )
        self.assertEqual(titles[index + 1], "Evidence Storage")

    def test_every_gate_name_appears_in_the_section(self):
        for name in GATE_NAMES:
            self.assertIn(f"`{name}`", self.section)

    def test_phase_table_lists_each_phase_once_with_its_switch(self):
        phases = _section_table(self.section, "### Phases")
        self.assertEqual([row[0].strip("`") for row in phases], list(PHASE_IDS))
        self.assertEqual(
            {gate["phase"]: gate["switch"] for gate in _gates()},
            {
                phase: switch
                for phase, switch in PHASE_SWITCHES.items()
                if phase != "score_source"
            },
        )
        for row in phases:
            phase = row[0].strip("`")
            with self.subTest(phase=phase):
                self.assertEqual(len(row), 5)
                if phase == "shell":
                    self.assertTrue(row[2].startswith("none"))
                    continue
                documented = row[2].split("`")[1]
                self.assertIn(documented, ALLOWED_CAPABILITY_KEYS)
                self.assertEqual(documented, PHASE_SWITCHES[phase])

    def test_phase_table_names_exist_in_the_repository(self):
        """Every setting, manifest and CronJob a phase row names is real."""
        phases = _section_table(self.section, "### Phases")
        flags = manifests = cronjobs = 0
        for row in phases:
            phase = row[0].strip("`")
            for name in re.findall(r"`([A-Z][A-Z0-9_]+)`", row[1]):
                with self.subTest(phase=phase, setting=name):
                    self.assertTrue(hasattr(settings, name), name)
                    flags += 1
            paths = re.findall(r"`((?:k8s|deploy)/[^`]+)`", row[3])
            for path in paths:
                with self.subTest(phase=phase, manifest=path):
                    self.assertTrue((REPO_ROOT / path).is_file(), path)
                    manifests += 1
            for cronjob in re.findall(r"\(`(crank-[a-z-]+)`\)", row[3]):
                with self.subTest(phase=phase, cronjob=cronjob):
                    self.assertEqual(len(paths), 1)
                    manifest = (REPO_ROOT / paths[0]).read_text(encoding="utf-8")
                    self.assertIn(f"  name: {cronjob}\n", manifest)
                    cronjobs += 1
        self.assertGreaterEqual(flags, 8)
        self.assertGreaterEqual(manifests, 6)
        self.assertEqual(cronjobs, 4)

    def test_master_flag_is_shared_and_has_its_own_rule(self):
        """``AGENT_RUN_ENABLED`` gates four phases' commands; say so."""
        from crank.management.base import AgentRunCommand
        from crank.management.commands import (
            gather_scores,
            recompute_matches,
            run_job_pipeline,
            schedule_crawls,
        )

        dependent = {
            module.Command.enabled_setting
            for module in (
                gather_scores, recompute_matches, run_job_pipeline, schedule_crawls
            )
        }
        for module in (
            gather_scores, recompute_matches, run_job_pipeline, schedule_crawls
        ):
            self.assertTrue(issubclass(module.Command, AgentRunCommand))
        self.assertEqual(
            dependent,
            {
                "GATHER_SCORES_ENABLED", "MATCH_RECOMPUTE_ENABLED",
                "JOB_PIPELINE_ENABLED", "CRAWL_CRON_ENABLED",
            },
        )
        rows = _section_table(self.section, "#### Shared master flag")
        self.assertEqual([row[0] for row in rows], [
            "Enable", "Roll back one phase", "Revert the master flag",
        ])
        self.assertIn("its own pull request", rows[0][1])
        self.assertIn("Leave `AGENT_RUN_ENABLED` alone", rows[1][1])
        self.assertTrue(rows[2][1].startswith("Last, and only when"))
        for name in dependent:
            self.assertIn(f"`{name}`", rows[2][1])
        phases = {
            row[0].strip("`"): row[1]
            for row in _section_table(self.section, "### Phases")
        }
        for phase in ("job_source", "match_recompute", "organization_crawl", "score_source"):
            self.assertIn("needs the master flag `AGENT_RUN_ENABLED`", phases[phase])
        self.assertIn("Revert phase flags in the reverse of the order", " ".join(self.section.split()))

    def test_decision_table_lists_each_phase_once_with_its_gates(self):
        decisions = _section_table(self.section, "### Decision gates per phase")
        self.assertEqual([row[0].strip("`") for row in decisions], list(PHASE_IDS))
        for row in decisions:
            phase = row[0].strip("`")
            expected = {g["name"] for g in _gates() if g["phase"] == phase}
            documented = set(re.findall(r"`([a-z-]+)`", row[1])) & set(GATE_NAMES)
            self.assertEqual(documented, expected, phase)

    def test_provisional_numbers_match_the_yaml(self):
        rows = _section_table(self.section, "### Provisional numbers for #492 gates")
        documented = {}
        for row in rows:
            number = row[1].split("`")[1]
            for name in re.findall(r"`([a-z-]+)`", row[0]):
                documented[name] = float(number)
        provisional = {
            gate["name"]: float(gate["threshold"])
            for gate in _gates()
            if gate["status"] == "provisional"
        }
        self.assertEqual(documented, provisional)

    def test_provisional_numbers_say_which_are_new(self):
        """Only the reply-success number predates #492; the rest are new."""
        rows = _section_table(self.section, "### Provisional numbers for #492 gates")
        origin = {}
        for row in rows:
            for name in re.findall(r"`([a-z-]+)`", row[0]):
                origin[name] = row[2]
        provisional = {g["name"] for g in _gates() if g["status"] == "provisional"}
        self.assertEqual(set(origin), provisional)
        carried = {name for name, value in origin.items() if value == "carried over"}
        self.assertEqual(carried, {"interactive-reply-success"})
        new = {name for name, value in origin.items() if value == "new in #492"}
        self.assertEqual(new, provisional - carried)
        # The carried-over number really is elsewhere in the document.
        before = ROLLOUT_DOC.read_text(encoding="utf-8").split(
            f"## {RELEASE_SECTION}"
        )[0]
        self.assertIn("90%+ success rate over window", before)
        self.assertNotIn("Carried-over thresholds", self.section)

    def test_decision_rows_state_the_window_their_gates_read(self):
        """A phase observed for 14 days whose gates read 24 hours says so."""
        decisions = _section_table(self.section, "### Decision gates per phase")
        windows = {}
        for gate in _gates():
            windows.setdefault(gate["phase"], set()).add(gate["window"])
        for row in decisions:
            phase = row[0].strip("`")
            for window in windows.get(phase, ()):
                with self.subTest(phase=phase, window=window):
                    self.assertIn(window, row[2])

    def test_section_states_the_required_facts(self):
        normalized = " ".join(self.section.split())
        for phrase in (
            "procedure, not a verified state",
            "https://github.com/norcalipa/crank/issues/453",
            "https://crank.fyi/healthz/ready/",
            "The shell has no switch",
            "Redeploy the previous image tag",
            "One pull request per capability",
            "k8s/crank-agent-config.yml",
            "`CapabilitySwitch`",
            "revert pull request",
            "defines no staging deployment",
            "### Decision record template",
            "Release SHA",
            "Enablement commit",
            "Post-merge check",
            "Sign-off",
            "`data_counts`",
            "`release_verdict`",
            "crank-healthcheck",
            "**No gate can pass today.**",
            "nothing in it creates the alert policy",
            "Alert policy check",
            "nothing schedules the drain",
            "no CronJob in `k8s/` or `deploy/` runs it",
            "5. Verify after the merge.",
            "Pods read `envFrom` values only when they start",
            "kubectl -n crank rollout restart deployment/crank",
            "kubectl -n crank rollout status deployment/crank --timeout=300s",
            "A held gate blocks expansion.",
            "Only `no_source` and `source_disabled` count as unavailable.",
            "`no-recent-success` is deliberately **not** in `job-source-alerts-quiet`",
        ):
            self.assertIn(phrase, normalized)

    def test_durable_enablement_is_stated_as_blocked_by_555(self):
        """No instruction may lead an operator to commit a flag before #555."""
        normalized = " ".join(self.section.split())
        rule = normalized.split("### Durable enablement rule")[1].split("### Phases")[0]
        opening = rule.split("**Why a cluster-only change does not last.**")[0]
        for phrase in (
            "not currently safe",
            "https://github.com/norcalipa/crank/issues/555",
            "which is open",
            "Do not merge an enablement pull request for any phase after `shell`"
            " until #555 is fixed.",
        ):
            self.assertIn(phrase, opening)
        for phrase in (
            "makes `GET /healthz/ready/` return **503**",
            "**new web pods never become Ready**",
            "**The deploy workflow still goes green**",
            "never put a credential in a ConfigMap or in the repository",
            "**Procedure once #555 is fixed.**",
            "They are not to be followed before then.",
            "an approving review is not the sign-off",
            "see decision D7",
            "**no image is built from the enablement merge commit**",
            "must return **HTTP 200**",
            '`"enabled": true` and `"ok": true`',
            "**A 503 is not fixed by another restart.**",
            "`concurrency: deployment` with `cancel-in-progress: true`",
        ):
            self.assertIn(phrase, rule)
        # The numbered procedure comes after the statement that it is blocked.
        self.assertLess(
            rule.index("not currently safe"), rule.index("1. One pull request")
        )
        self.assertLess(
            rule.index("**Procedure once #555 is fixed.**"),
            rule.index("1. One pull request"),
        )
        self.assertNotIn("enabled durably by a **commit**", normalized)
        self.assertNotIn("the approving review is the named sign-off", normalized)
        criteria = normalized.split(
            "Entry criteria that the repository does not yet satisfy:"
        )[1].split("| Phase id | Gates |")[0]
        self.assertIn("**Every phase after `shell`: [#555]", criteria)
        self.assertIn("A hard precondition.", criteria)

    def test_observed_state_records_the_blanked_secret(self):
        rows = _section_table(
            self.section, "### Observed state when this section was written"
        )
        facts = {row[0]: row[1] for row in rows}
        blank = facts["Capability credentials are blanked on every deploy"]
        for key in (
            "LLM_API_KEY", "YELP_API_KEY", "USAJOBS_AUTH_KEY",
            "USAJOBS_USER_AGENT_EMAIL", "FIRECRAWL_API_KEY",
        ):
            self.assertIn(f"`{key}`", blank)
        self.assertIn("issues/555", blank)
        self.assertIn(
            "The readiness probe fails on an enabled, unconfigured capability", facts
        )
        self.assertIn(
            "The job-pipeline and match-recompute CronJobs are not created by any"
            " deploy",
            facts,
        )

    def test_deploy_workflows_blank_the_capability_secret(self):
        """The observed defect (#555). Update the docs when this stops holding."""
        workflows = REPO_ROOT / ".github" / "workflows"
        for name in ("deploy-home.yml", "update-home-deployment.yml"):
            source = (workflows / name).read_text(encoding="utf-8")
            with self.subTest(workflow=name):
                self.assertIn(
                    "create secret generic crank-capability-secrets", source
                )
                for key in (
                    "LLM_API_KEY", "YELP_API_KEY", "USAJOBS_AUTH_KEY",
                    "USAJOBS_USER_AGENT_EMAIL", "FIRECRAWL_API_KEY",
                ):
                    self.assertIn(f"--from-literal={key}=''", source)
                self.assertIn("--dry-run=client -o yaml | k3s kubectl apply -f -", source)
                self.assertNotIn("rollout status", source)
                self.assertIn("group: deployment", source)
                self.assertIn("cancel-in-progress: true", source)

    def test_readiness_probe_and_secret_precedence_in_the_web_manifest(self):
        manifest = (REPO_ROOT / "k8s" / "crank.yml").read_text(encoding="utf-8")
        probe = manifest.split("readinessProbe:", 1)[1][:120]
        self.assertIn("path: /healthz/ready/", probe)
        refs = re.findall(r"(configMapRef|secretRef):\s+name: ([a-z-]+)", manifest)
        web = refs[: refs.index(("secretRef", "crank-capability-secrets")) + 1]
        self.assertEqual(web[-1], ("secretRef", "crank-capability-secrets"))
        self.assertIn(("configMapRef", "crank-agent-config"), web[:-1])

    def test_enablement_merges_build_no_image(self):
        build = (REPO_ROOT / ".github" / "workflows" / "build-image.yml").read_text(
            encoding="utf-8"
        )
        ignored = build.split("paths-ignore:", 1)[1].split("concurrency:", 1)[0]
        self.assertIn("- 'k8s/**'", ignored)

    def test_deploy_cronjobs_are_unapplied_templates_without_source_secrets(self):
        workflows = REPO_ROOT / ".github" / "workflows"
        for path in sorted(workflows.glob("*.yml")):
            self.assertNotIn("deploy/", path.read_text(encoding="utf-8"), path.name)
        for name in ("cronjob-job-pipeline.yaml", "cronjob-match-recompute.yaml"):
            text = (REPO_ROOT / "deploy" / name).read_text(encoding="utf-8")
            with self.subTest(manifest=name):
                self.assertIn("crank:${GITHUB_SHA}", text)
                self.assertIn("imagePullPolicy: Always", text)
                self.assertIn("suspend: true", text)
                self.assertNotIn("crank-capability-secrets", text)

    def test_decision_record_template_fields(self):
        rows = _section_table(self.section, "### Decision record template")
        self.assertEqual(
            [row[0] for row in rows],
            [
                "Phase id", "Decision", "Enablement commit", "Release SHA",
                "Readiness record", "Source / fixture readiness", "Post-merge check",
                "Gate results", "Alert policy check", "Failures", "Follow-up fixes",
                "Rollback evidence", "Sign-off",
            ],
        )
        fields = {row[0]: row[1] for row in rows}
        self.assertIn("`source_version`", fields["Release SHA"])
        self.assertIn("not the enablement commit", fields["Release SHA"])
        self.assertIn("decision D5", fields["Sign-off"])
        self.assertIn("the step that decided", fields["Gate results"])

    def test_verification_step_matches_the_deploy_workflows(self):
        """The rule's claims about image tags are read from the workflows."""
        workflows = REPO_ROOT / ".github" / "workflows"
        update = (workflows / "update-home-deployment.yml").read_text(encoding="utf-8")
        deploy = (workflows / "deploy-home.yml").read_text(encoding="utf-8")
        self.assertIn("export GITHUB_SHA=latest", update)
        self.assertIn("export GITHUB_SHA=${{ github.event.workflow_run.head_sha }}", deploy)
        for workflow in (update, deploy):
            self.assertIn("envsubst < /tmp/crank-agent-config.yml", workflow)
            self.assertNotIn("rollout restart", workflow)
        manifest = (REPO_ROOT / "k8s" / "crank.yml").read_text(encoding="utf-8")
        self.assertIn("image: ghcr.io/norcalipa/crank/crank:${GITHUB_SHA}", manifest)
        self.assertIn("envFrom:", manifest)

    def test_publication_sweep_has_no_schedule_in_the_repository(self):
        """The entry criterion stays true until a CronJob runs the sweep."""
        manifests = [
            path
            for folder in ("k8s", "deploy")
            for path in sorted((REPO_ROOT / folder).iterdir())
            if "secret" not in path.name
        ]
        self.assertTrue(manifests)
        for path in manifests:
            with self.subTest(path=path.name):
                self.assertNotIn(
                    "publication_sweep", path.read_text(encoding="utf-8")
                )


def _section_table(section: str, heading: str) -> list:
    """Body rows of the first table under a heading inside a section."""
    assert section.count(f"\n{heading}\n") == 1, heading
    return _table_after(section, f"\n{heading}\n")


class ReadinessProbeConsequenceTests(TestCase):
    """What the readiness endpoint returns for the configurations the docs warn about."""

    CONFIGURED = {
        "INTERACTIVE_AGENT_ENABLED": True,
        "LLM_PROVIDER": "anthropic",
        "LLM_MODEL": "a-model",
        "LLM_API_KEY": "not-a-real-key-492",
    }

    def _ready(self):
        return self.client.get("/healthz/ready/")

    def _capability(self, response, name: str) -> dict:
        return next(
            cap
            for cap in response.json()["capabilities"]["capabilities"]
            if cap["name"] == name
        )

    def test_enabled_flag_with_a_blank_key_is_not_ready(self):
        """The #555 consequence: flag committed, credential blanked by a deploy."""
        with override_settings(**dict(self.CONFIGURED, LLM_API_KEY="")):
            response = self._ready()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")
        entry = self._capability(response, "interactive_agent")
        # ``enabled: true`` alone is the flag, not a working capability.
        self.assertIs(entry["enabled"], True)
        self.assertIs(entry["ok"], False)
        self.assertTrue(entry["issues"])
        self.assertIs(response.json()["capabilities"]["all_ok"], False)

    def test_enabled_flag_with_its_configuration_is_ready(self):
        with override_settings(**self.CONFIGURED):
            response = self._ready()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        entry = self._capability(response, "interactive_agent")
        self.assertEqual((entry["enabled"], entry["ok"]), (True, True))
        self.assertIs(response.json()["capabilities"]["all_ok"], True)

    def test_phase_flag_without_the_master_flag_is_not_ready(self):
        """Reverting ``AGENT_RUN_ENABLED`` before the flags that need it."""
        for flag in ("JOB_PIPELINE_ENABLED", "CRAWL_CRON_ENABLED"):
            with self.subTest(flag=flag):
                with override_settings(AGENT_RUN_ENABLED=False, **{flag: True}):
                    self.assertEqual(self._ready().status_code, 503)
                with override_settings(AGENT_RUN_ENABLED=True, **{flag: True}):
                    self.assertEqual(self._ready().status_code, 200)

    def test_master_flag_alone_is_ready(self):
        with override_settings(
            AGENT_RUN_ENABLED=True,
            JOB_PIPELINE_ENABLED=False,
            CRAWL_CRON_ENABLED=False,
            INTERACTIVE_AGENT_ENABLED=False,
        ):
            self.assertEqual(self._ready().status_code, 200)


class RunbookDurableEnablementTests(SimpleTestCase):
    """The crawl runbooks only instruct steps that work, and say what does not."""

    RUNBOOKS = ("runbook-initial-crawl.md", "runbook-crawl-scheduling.md")
    REAPPLIED = {
        "crank-crawl-cron.yaml": "crank-crawl-organizations",
        "crank-healthcheck-cron.yaml": "crank-healthcheck",
    }
    #: A shell line that changes a ConfigMap, in any spelling.
    CONFIGMAP_WRITE = re.compile(
        r"kubectl[^\n]*\b(edit|patch|create|replace|set|apply)\b[^\n]*configmap",
        re.IGNORECASE,
    )
    #: A credential setting given a value, as in ``FIRECRAWL_API_KEY=<key>``.
    CREDENTIAL_ASSIGNMENT = re.compile(
        r"\b[A-Z_]*(API_KEY|AUTH_KEY|SECRET|PASSWORD|TOKEN)[A-Z_]*\s*[=:]\s*\S"
    )

    def _text(self, name: str) -> str:
        return (REPO_ROOT / "docs" / name).read_text(encoding="utf-8")

    def _commands(self, name: str) -> list:
        """Lines inside fenced code blocks: what an operator would paste."""
        lines, inside = [], False
        for line in self._text(name).splitlines():
            if line.strip().startswith("```"):
                inside = not inside
            elif inside:
                lines.append(line.strip())
        return lines

    def test_reapplied_cronjobs_are_suspended_templates(self):
        """Why a patch is undone and a raw apply is invalid."""
        workflows = REPO_ROOT / ".github" / "workflows"
        for workflow in ("deploy-home.yml", "update-home-deployment.yml"):
            source = (workflows / workflow).read_text(encoding="utf-8")
            self.assertNotIn("deploy/", source)
            for manifest in self.REAPPLIED:
                self.assertIn(f"envsubst < /tmp/{manifest}", source)
        for manifest, cronjob in self.REAPPLIED.items():
            text = (REPO_ROOT / "k8s" / manifest).read_text(encoding="utf-8")
            self.assertIn(f"name: {cronjob}", text)
            self.assertIn("suspend: true", text)
            self.assertIn("crank:${GITHUB_SHA}", text)

    def test_runbooks_do_not_patch_or_raw_apply_reapplied_cronjobs(self):
        for name in self.RUNBOOKS:
            text = self._text(name)
            with self.subTest(runbook=name):
                self.assertNotRegex(text, r"kubectl[^\n]*apply[^\n]*k8s/")
                for cronjob in self.REAPPLIED.values():
                    self.assertNotRegex(
                        text, rf"patch\s+cronjobs?[\s/]+{re.escape(cronjob)}\b"
                    )
                self.assertIn("patch cronjob crank-job-pipeline", text)
                self.assertIn("deploy/cronjob-job-pipeline.yaml", text)

    def test_every_raw_apply_substitutes_the_image_tag(self):
        """``${GITHUB_SHA}`` is literal in every manifest; never apply one raw."""
        applies = 0
        for name in self.RUNBOOKS:
            for line in self._commands(name):
                if "kubectl" in line and " apply " in f" {line} ":
                    with self.subTest(runbook=name, line=line):
                        self.assertNotRegex(line, r"apply[^|]*-f\s+(k8s|deploy)/")
                        self.assertIn("envsubst '${GITHUB_SHA}' < deploy/", line)
                        self.assertTrue(line.startswith("GITHUB_SHA=latest "))
                        applies += 1
        self.assertEqual(applies, 1)

    def test_runbooks_never_write_a_configmap_or_assign_a_credential(self):
        for name in self.RUNBOOKS:
            text = self._text(name)
            with self.subTest(runbook=name):
                self.assertNotRegex(text, self.CONFIGMAP_WRITE)
                self.assertNotRegex(text, self.CREDENTIAL_ASSIGNMENT)
                self.assertNotIn("crank-secrets", text.replace("crank-capability-secrets", ""))
        self.assertRegex("kubectl -n crank edit configmap crank-agent-config", self.CONFIGMAP_WRITE)
        self.assertRegex("FIRECRAWL_API_KEY=<your-key>", self.CREDENTIAL_ASSIGNMENT)
        self.assertRegex('LLM_API_KEY: "x"', self.CREDENTIAL_ASSIGNMENT)

    def test_runbooks_say_what_cannot_be_done_until_555(self):
        for name in self.RUNBOOKS:
            normalized = " ".join(self._text(name).split())
            with self.subTest(runbook=name):
                self.assertIn("https://github.com/norcalipa/crank/issues/555", normalized)
                self.assertIn("Do not commit a capability flag", normalized)
                self.assertIn("`crank-capability-secrets`", normalized)
                self.assertIn("gives no command for it", normalized)
            # A paragraph that tells the operator to commit an enablement
            # names #555. The read-only health probe is the one exception.
            instructions = 0
            for paragraph in re.split(r"\n\s*\n", self._text(name)):
                flat = " ".join(paragraph.split())
                if not re.search(
                    r"[Cc]ommit(?:ting)? `spec\.suspend: false`|_ENABLED: \"true\"", flat
                ):
                    continue
                instructions += 1
                with self.subTest(runbook=name, paragraph=flat[:60]):
                    if "k8s/crank-healthcheck-cron.yaml" in flat:
                        self.assertIn("does not wait for #555", flat)
                    else:
                        self.assertIn("#555", flat)
            self.assertGreaterEqual(instructions, 2, name)
        initial = " ".join(self._text(self.RUNBOOKS[0]).split())
        for phrase in (
            "## Status: cannot be completed durably today",
            "**The repository does not create `crank-job-pipeline`.**",
            "returns `NotFound`",
            "its image tag is the literal text `${GITHUB_SHA}`",
            "**The image tag is fixed at whatever you substituted, because nothing"
            " re-applies this manifest.**",
            "With a commit SHA the pipeline keeps running that build indefinitely",
            "**Its pods receive no source credential.**",
            "**Never put a credential in the `crank-agent-config` ConfigMap or in"
            " any file in the repository.**",
            "**Do not do this until #555 is fixed.**",
            "Revert the master flag `AGENT_RUN_ENABLED` last",
        ):
            self.assertIn(phrase, initial)

    def test_runbooks_name_the_files_and_the_switch(self):
        for name in self.RUNBOOKS:
            normalized = " ".join(self._text(name).split())
            with self.subTest(runbook=name):
                for phrase in (
                    "k8s/crank-agent-config.yml",
                    "k8s/crank-crawl-cron.yaml",
                    "`spec.suspend: false`",
                    "`CapabilitySwitch`",
                    "`job_pipeline`",
                    "`crawl_schedule`",
                    "Durable enablement rule",
                    "`AGENT_RUN_ENABLED`",
                ):
                    self.assertIn(phrase, normalized)
        initial = " ".join(self._text(self.RUNBOOKS[0]).split())
        self.assertIn("k8s/crank-healthcheck-cron.yaml", initial)
        self.assertIn("revert the commit", initial)

    def test_panel_table_points_at_existing_steps(self):
        text = self._text(self.RUNBOOKS[0])
        headings = {line[3:] for line in text.splitlines() if line.startswith("## ")}
        rows = _table_after(text, "## Verify with the readiness panel")
        steps = [row[1] for row in rows if row[1].startswith("Step ")]
        self.assertGreaterEqual(len(steps), 6)
        for step in steps:
            self.assertIn(step, headings)


class CapabilityRegistryRowTests(SimpleTestCase):
    """Registered rows are exactly ALLOWED_CAPABILITY_KEYS (issue #463)."""

    def _rows(self) -> dict:
        section = _section(
            ROLLOUT_DOC, "Capability Registry (Epic #454 — issue #463)"
        )
        return {
            row[1].split("`")[1]: row[4] for row in _table_rows(section)[1:]
        }

    def test_registered_rows_match_the_code_registry(self):
        rows = self._rows()
        registered = {key for key, status in rows.items() if status == "registered"}
        self.assertEqual(registered, set(ALLOWED_CAPABILITY_KEYS))

    def test_unregistered_names_are_planned(self):
        rows = self._rows()
        planned = {key for key, status in rows.items() if status == "planned"}
        self.assertEqual(planned, {"assistant_shell"})
        self.assertFalse(planned & set(ALLOWED_CAPABILITY_KEYS))
        self.assertEqual(set(rows.values()), {"registered", "planned"})

    def test_publication_consumer_row_names_its_settings_flag(self):
        content = ROLLOUT_DOC.read_text(encoding="utf-8")
        self.assertIn(
            "| `publication_consumer` + `PUBLICATION_CONSUMER_ENABLED` | #470 |"
            " off | registered |",
            content,
        )


class UsabilityValidationDocTests(SimpleTestCase):
    """The protocol kit has the sections and rules #492 depends on."""

    def setUp(self):
        self.content = USABILITY_DOC.read_text(encoding="utf-8")
        self.normalized = " ".join(self.content.split())

    def test_license_header(self):
        self.assertTrue(self.content.startswith("<!-- Copyright (c) 2024 Isaac Adams"))

    def test_required_sections_each_appear_once(self):
        headings = [
            line[3:] for line in self.content.splitlines() if line.startswith("## ")
        ]
        for title in (
            "Decisions pending owner confirmation",
            "Preconditions",
            "Participants and recruiting criteria",
            "Consent and recording checklist",
            "Data-minimisation rules",
            "Moderator script",
            "Task list",
            "Comprehension probes",
            "Measures",
            "Severity rubric",
            "Findings template",
            "Pass criteria",
            "Failure and retry matrix",
            "What only the owner can do",
            "Results record",
        ):
            self.assertEqual(headings.count(title), 1, title)

    def test_tasks_and_probes_are_defined(self):
        tasks = _table_rows(_section(USABILITY_DOC, "Task list"))[1:]
        self.assertEqual(
            [(row[0], row[1]) for row in tasks],
            [
                (
                    "T1",
                    '"Tell the product what matters to you in a job, using the'
                    ' invented priorities we agreed."',
                ),
                (
                    "T2",
                    '"Pick one result. Tell me why the product thinks it suits you,'
                    ' and when the information behind that reason was last checked."',
                ),
                (
                    "T3",
                    '"Find a job at that company that is open now and get to the'
                    ' place where you could apply."',
                ),
                (
                    "T4",
                    '"Find one thing about this company or job that the product does'
                    ' not know, or that may be out of date."',
                ),
            ],
        )
        for row, observed in zip(
            tasks,
            ("priorities are saved", "states a reason", "original posting",
             "unknown or stale"),
        ):
            self.assertEqual(len(row), 3, row[0])
            self.assertIn(observed, row[2], row[0])
        probes = _table_rows(_section(USABILITY_DOC, "Comprehension probes"))[1:]
        self.assertEqual(
            [row[0] for row in probes],
            [
                "C1 — company score", "C2 — personal fit", "C3 — data coverage",
                "C4 — uncertainty",
            ],
        )
        for row, question, rubric in zip(
            probes,
            (
                "What does this company's score tell you?",
                "What does the product mean when it says this suits you?",
                "How much does the product know about this company?",
                "Is there anything here you would check yourself",
            ),
            (
                "the same for everyone",
                "comes from the priorities they set",
                "does not read a missing fact as a bad fact",
                "unverified or possibly out of date",
            ),
        ):
            self.assertEqual(len(row), 3, row[0])
            self.assertIn(question, row[1], row[0])
            self.assertIn(rubric, row[2], row[0])
        self.assertIn("T1 to T3, in order, are the **core task**", self.normalized)

    def test_consent_checklist_items(self):
        """Each item is asserted, not only the heading."""
        section = _section(USABILITY_DOC, "Consent and recording checklist")
        items = [" ".join(item.split()) for item in section.split("- [ ]")[1:]]
        self.assertEqual(len(items), 8)
        for item, phrase in zip(
            items,
            (
                "can stop at any time without giving a reason",
                "knows what is written down: task outcomes, timings, probe answers",
                "what the moderator does not write down",
                "**the product itself keeps what they type**",
                "**not to type real personal details**",
                "Audio or screen recording happens only with separate explicit"
                " agreement",
                "invented priorities rather than their real ones",
                "using a throwaway account",
            ),
        ):
            self.assertIn(phrase, item)
        self.assertIn(
            "Before each session, the moderator confirms aloud and ticks:",
            " ".join(section.split()),
        )

    def test_consent_states_what_the_product_stores(self):
        """The consent line may not promise what the product does not do."""
        consent = " ".join(
            _section(USABILITY_DOC, "Consent and recording checklist").split()
        )
        self.assertNotIn("anything they typed", consent)
        self.assertNotIn("what is not kept", consent)
        for phrase in (
            "chat messages and saved priorities are stored under the throwaway"
            " account",
            "each message is sent to the external language-model provider",
            "the copy sent to the provider cannot be deleted from here",
        ):
            self.assertIn(phrase, consent)
        rules = " ".join(_section(USABILITY_DOC, "Data-minimisation rules").split())
        for phrase in (
            "### What the product stores during a session",
            "stored verbatim (`JobSearchMessage.content`)",
            "The newest 50 messages of each conversation are kept"
            " (`JOB_SEARCH_MESSAGES_RETENTION`), with no time limit.",
            "Saved priorities are stored (`UserPreference`)",
        ):
            self.assertIn(phrase, rules)
        self.assertEqual(settings.JOB_SEARCH_MESSAGES_RETENTION, 50)
        field = JobSearchMessage._meta.get_field("content")
        self.assertEqual(field.get_internal_type(), "TextField")

    def test_session_data_deletion_step(self):
        rules = " ".join(_section(USABILITY_DOC, "Data-minimisation rules").split())
        deletion = rules.split("### Deleting session data")[1]
        for phrase in (
            "After the last session, and before the results record is merged:",
            "A superuser deletes each throwaway account in Django admin",
            "**The repository owner confirms the deletion**",
            "`agent_conversation_delete`",
            "There is no command or page that deletes a set of accounts in one"
            " step, and none is added here.",
            "file a follow-up issue",
            "Nothing here can delete what was already sent to the model provider.",
        ):
            self.assertIn(phrase, deletion)
        checklist = " ".join(
            _section(USABILITY_DOC, "What only the owner can do").split()
        )
        self.assertIn("Delete the throwaway accounts and confirm the deletion", checklist)
        self.assertIn("Check the model provider's retention terms", checklist)
        record = _section(USABILITY_DOC, "Results record")
        self.assertIn("| Session accounts deleted (date, confirmed by) | _…_ |", record)
        self.assertIn("| Raw notes and recordings destroyed (date) | _…_ |", record)

    def test_assistance_is_defined(self):
        script = " ".join(_section(USABILITY_DOC, "Moderator script").split())
        self.assertIn("**Assistance** is any moderator action", script)
        self.assertIn("These are not assistance", script)

    def test_pass_criteria(self):
        criteria = " ".join(_section(USABILITY_DOC, "Pass criteria").split())
        self.assertIn(
            "at least 4 of 5 participants complete T1–T3 unassisted", criteria
        )
        self.assertIn(
            "at least 4 of 5 participants answer all four probes (C1–C4) correctly",
            criteria,
        )

    def test_forbidden_data_and_retention(self):
        rules = " ".join(_section(USABILITY_DOC, "Data-minimisation rules").split())
        self.assertIn(
            "The following are never recorded in the repository, in an issue or"
            " pull request, or in the results record:",
            rules,
        )
        listed = rules.split("or in the results record:")[1].split("Further rules:")[0]
        self.assertEqual(
            [item.strip() for item in listed.split("- ")[1:]],
            [
                "names;",
                "e-mail addresses;",
                "employers;",
                "real compensation figures;",
                "real preference values;",
                "account identifiers;",
                "prompts and replies verbatim;",
                "screen recordings stored in the repository.",
            ],
        )
        self.assertIn("recorded as P1–P5 and nothing else", rules)
        self.assertIn("never quoted from what the participant typed", rules)
        self.assertIn("Retention limit for raw notes", rules)

    def test_retention_has_a_date_cap_restated_from_d6(self):
        """An event-only limit keeps notes for ever if the round is abandoned."""
        decisions = self._decisions()
        cap = re.search(r"no later than (\d+ days) after the last session", decisions["D6"])
        self.assertIsNotNone(cap)
        rules = " ".join(_section(USABILITY_DOC, "Data-minimisation rules").split())
        limit = rules.split("**Retention limit for raw notes:**")[1].split("###")[0]
        self.assertIn(f"no later than {cap.group(1)} after the last session (D6)", limit)
        self.assertIn("abandoned", limit)
        self.assertIn("abandoned", decisions["D6"])

    def test_pass_bar_is_restated_from_d4(self):
        """The decision row and the criteria must carry the same numbers."""
        bar = re.search(
            r"at least (\d) of (\d) participants answer all four", self._decisions()["D4"]
        )
        self.assertIsNotNone(bar)
        criteria = " ".join(_section(USABILITY_DOC, "Pass criteria").split())
        self.assertIn(
            f"the bar in D4 — at least {bar.group(1)} of {bar.group(2)} participants"
            " answer all four probes (C1–C4) correctly",
            criteria,
        )
        self.assertEqual(bar.groups(), ("4", "5"))

    def test_sign_off_has_one_mechanism(self):
        decisions = self._decisions()
        self.assertIn("signs each role by GitHub handle and date", decisions["D5"])
        self.assertIn("an approving review is not one", decisions["D5"])
        rollout = " ".join(_section(ROLLOUT_DOC, RELEASE_SECTION).split())
        self.assertNotIn("approving review is the named sign-off", rollout)
        self.assertIn("which is public", decisions["D7"])
        self.assertIn("`data_counts` and gate values are accepted as public", decisions["D7"])

    def test_preconditions_name_the_blocking_issues(self):
        section = _section(USABILITY_DOC, "Preconditions")
        rows = _table_rows(section)
        self.assertEqual(
            rows[0], ["Blocking issue", "What it changes", "Affects", "State on 2026-10-08"]
        )
        self.assertEqual(
            [row[0] for row in rows[1:]],
            ["#551", "#489", "#486", "#488", "#536", "#537", "#548", "#555"],
        )
        for row in rows[1:]:
            self.assertTrue(row[1] and row[2], row[0])
            self.assertTrue(row[3].startswith("open"), row[0])
        normalized = " ".join(section.split())
        self.assertIn(
            "every issue in the table below is closed and its change is in the"
            " release under test",
            normalized,
        )
        self.assertIn("https://github.com/norcalipa/crank/issues/555", normalized)
        self.assertIn("nobody should commit a capability flag to try", normalized)
        checklist = " ".join(
            _section(USABILITY_DOC, "What only the owner can do").split()
        )
        self.assertIn("do not commit a capability flag before it is fixed", checklist)
        self.assertIn("Once #555 is fixed, enable each phase durably", checklist)

    def _decisions(self) -> dict:
        section = _section(USABILITY_DOC, "Decisions pending owner confirmation")
        rows = _table_rows(section.split(FLOORS_HEADING)[0])[1:]
        return {row[0]: row[2] for row in rows}

    def test_participants_cover_fresh_and_returning(self):
        section = " ".join(
            _section(USABILITY_DOC, "Participants and recruiting criteria").split()
        )
        self.assertIn("Five participants", section)
        self.assertIn("**fresh**", section)
        self.assertIn("**returning**", section)

    def test_decisions_are_marked_as_defaults_in_one_place(self):
        section = _section(USABILITY_DOC, "Decisions pending owner confirmation")
        rows = _table_rows(section.split(FLOORS_HEADING)[0])[1:]
        self.assertEqual(
            [(row[0], row[1]) for row in rows],
            [
                ("D1", "Shell rollback"),
                ("D2", "Gate thresholds"),
                ("D3", "Where sessions run"),
                ("D4", "Pass bar for A2"),
                ("D5", "Sign-offs and where evidence lives"),
                ("D6", "How long raw notes and recordings are kept"),
                ("D7", "Where decision records live and what they may show"),
            ],
        )
        for row in rows:
            self.assertTrue(row[2].startswith("**default — owner may change:**"), row[0])
        marker = "**default — owner may change:**"
        self.assertEqual(self.content.count(marker), 7)
        self.assertNotIn(marker, ROLLOUT_DOC.read_text(encoding="utf-8"))
        # One table of decisions: no other document keeps its own list.
        for path in (ROLLOUT_DOC, MONITORING_DOC):
            self.assertNotIn(
                "owner may change", path.read_text(encoding="utf-8"), path.name
            )

    def test_sample_floors_are_listed_as_pending_without_a_default(self):
        """Every gate's floor is the owner's to set; none is defaulted."""
        section = _section(USABILITY_DOC, "Decisions pending owner confirmation")
        self.assertEqual(section.count(FLOORS_HEADING), 1)
        floors = section.split(FLOORS_HEADING)[1]
        self.assertIn("**no default**", floors)
        self.assertNotIn("default — owner may change", floors)
        rows = _table_rows(floors)
        self.assertEqual(
            rows[0],
            ["Gate", "What the floor counts", "Window", "Floor N", "Alert policy"],
        )
        gates = {gate["name"]: gate for gate in _gates()}
        self.assertEqual(
            [row[0].strip("`") for row in rows[1:]], [g["name"] for g in _gates()]
        )
        for row in rows[1:]:
            gate = gates[row[0].strip("`")]
            with self.subTest(gate=gate["name"]):
                self.assertTrue(row[1])
                self.assertEqual(row[2], gate["window"])
                self.assertEqual(
                    row[3], "not set" if gate["min_sample"] is None else "locked"
                )
                if gate["kind"] == "alerts_quiet":
                    self.assertIn(f"`{gate['signal_event']}`", row[1])
                    self.assertIn("live signal", row[1])
                    self.assertEqual(row[4], "not confirmed")
                else:
                    self.assertIn(f"`{gate['event']}`", row[1])
                    self.assertEqual(row[4], "—")

    def test_floor_descriptions_name_the_filters_that_narrow_them(self):
        section = _section(USABILITY_DOC, "Decisions pending owner confirmation")
        rows = {
            row[0].strip("`"): row[1]
            for row in _table_rows(section.split(FLOORS_HEADING)[1])[1:]
        }
        self.assertIn("`status = 'provider_succeeded'`", rows["interactive-alerts-quiet"])
        self.assertIn("degraded probe event does not count", rows["job-source-alerts-quiet"])
        for name in ("publication-to-match-lag", "matching-alerts-quiet"):
            self.assertIn("from the `match_recompute` stage", rows[name])
        self.assertIn("signed-out excluded", rows["assistant-ready-share"])

    def test_results_record_is_empty(self):
        record = _section(USABILITY_DOC, "Results record")
        outcomes = [row for row in _table_rows(record) if re.fullmatch(r"P[1-5]", row[0])]
        self.assertEqual([row[0] for row in outcomes], ["P1", "P2", "P3", "P4", "P5"])
        for row in outcomes:
            self.assertEqual(set(row[1:]), {"_…_"})


class FailureRetryMatrixTests(SimpleTestCase):
    """Every test cited as matrix evidence exists under that name."""

    def setUp(self):
        self.matrix = _section(USABILITY_DOC, "Failure and retry matrix")
        self.rows = _table_rows(self.matrix)[1:]

    def test_matrix_has_outcome_and_evidence_columns(self):
        header = _table_rows(self.matrix)[0]
        self.assertEqual(
            header,
            [
                "Failure mode",
                "Recovery action",
                "Draft preserved",
                "No duplicate turn",
                "Evidence",
            ],
        )
        self.assertGreaterEqual(len(self.rows), 9)
        for row in self.rows:
            self.assertEqual(len(row), 5, row[0])
            # "yes", optionally with how; never a hedge.
            self.assertRegex(row[2], r"^yes( — [^—]+)?$", row[0])
            self.assertRegex(row[3], r"^yes( — [^—]+)?$", row[0])
            for cell in (row[2], row[3]):
                self.assertNotRegex(cell, r"theory|unverified|never verified|probably")
            self.assertTrue(_CITATION.findall(row[4]), row[0])
        self.assertEqual(
            [row[0] for row in self.rows],
            [
                "Provider error (503 `assistant_unavailable`)",
                "Provider timeout",
                "Network failure before the server received the turn",
                "Stop while a turn is in flight",
                "Double-click on Retry",
                "Retry limit reached",
                "Reload during a failed turn",
                "Sign-in handoff or session expiry",
                "Second tab",
            ],
        )

    def test_every_cited_test_exists(self):
        citations = _CITATION.findall("\n".join(row[4] for row in self.rows))
        self.assertGreater(len(citations), 15)
        for path, name in citations:
            with self.subTest(path=path, name=name):
                source = (REPO_ROOT / path).read_text(encoding="utf-8")
                if path.endswith(".py"):
                    self.assertIn(f"def {name}(", source)
                else:
                    self.assertIn(f"test('{name}'", source)

    def test_skipped_browser_tests_are_marked_pending_not_passing(self):
        """A Playwright citation that is still skipped must say so."""
        for row in self.rows:
            for path, name in _CITATION.findall(row[4]):
                if not path.startswith("e2e/"):
                    continue
                with self.subTest(name=name):
                    source = (REPO_ROOT / path).read_text(encoding="utf-8")
                    body = source.split(f"test('{name}'", 1)[1][:400]
                    skipped = "test.skip(" in body or "pendingTicketMerge(" in body
                    before = row[4].split(f"`{path}::{name}`", 1)[0]
                    marked = before.rstrip().endswith(
                        "Browser evidence pending 492c:"
                    )
                    self.assertEqual(skipped, marked)

    @staticmethod
    def _python_test_is_skipped_by_decorator(source: str, name: str) -> bool:
        """True when the test, or its class, carries a skip decorator."""
        lines = source.splitlines()
        index = next(
            i for i, line in enumerate(lines) if line.lstrip().startswith(f"def {name}(")
        )
        owners = [index]
        if lines[index].startswith(" "):
            owners.append(
                next(i for i in range(index, -1, -1) if lines[i].startswith("class "))
            )
        for owner in owners:
            cursor = owner - 1
            while cursor >= 0 and lines[cursor].lstrip().startswith("@"):
                if "skip" in lines[cursor].lower():
                    return True
                cursor -= 1
        return False

    def test_conditionally_skipped_python_tests_are_marked_operator_run(self):
        """A pytest that only runs on MySQL is not evidence from CI."""
        marked_total = 0
        for row in self.rows:
            for path, name in _CITATION.findall(row[4]):
                if not path.endswith(".py"):
                    continue
                with self.subTest(name=name):
                    source = (REPO_ROOT / path).read_text(encoding="utf-8")
                    skipped = self._python_test_is_skipped_by_decorator(source, name)
                    before = row[4].split(f"`{path}::{name}`", 1)[0]
                    marked = before.rstrip().endswith("operator-run on MySQL:")
                    self.assertEqual(skipped, marked)
                    marked_total += marked
        self.assertEqual(marked_total, 1)

    def test_skip_detection_reads_function_and_class_decorators(self):
        source = (
            "@skipUnless(x, 'why')\n"
            "class A:\n"
            "    def test_in_skipped_class(self):\n"
            "        pass\n"
            "class B:\n"
            "    @other\n"
            "    @pytest.mark.skipif(x)\n"
            "    def test_skipped(self):\n"
            "        pass\n"
            "    def test_runs(self):\n"
            "        pass\n"
            "def test_module_level():\n"
            "    pass\n"
        )
        check = self._python_test_is_skipped_by_decorator
        self.assertTrue(check(source, "test_in_skipped_class"))
        self.assertTrue(check(source, "test_skipped"))
        self.assertFalse(check(source, "test_runs"))
        self.assertFalse(check(source, "test_module_level"))


class SessionAccountDeletionTests(TestCase):
    """The existing delete path the protocol names removes what a session stored."""

    def test_user_model_can_be_deleted_in_django_admin(self):
        """Users are registered in the admin and a superuser may delete them."""
        users = get_user_model()
        self.assertIn(users, admin.site._registry)
        request = RequestFactory().get("/admin/")
        request.user = users.objects.create_superuser(
            "owner-492", "owner@example.test", "not-real-492"
        )
        model_admin = admin.site._registry[users]
        self.assertTrue(model_admin.has_delete_permission(request))
        request.user = users.objects.create_user("staff-492", is_staff=True)
        self.assertFalse(model_admin.has_delete_permission(request))

    def test_deleting_the_account_deletes_conversations_messages_and_priorities(self):
        users = get_user_model()
        session = users.objects.create_user("p1-throwaway", password="not-real-492")
        other = users.objects.create_user("someone-else", password="not-real-492")
        for owner in (session, other):
            conversation = JobSearchConversation.objects.create(owner=owner)
            for index in range(2):
                JobSearchMessage.objects.create(
                    conversation=conversation,
                    role=JobSearchMessage.Role.USER,
                    content=f"synthetic turn {index}",
                )
            UserPreference.objects.get_or_create(user=owner)

        session.delete()

        self.assertFalse(JobSearchConversation.objects.filter(owner_id=session.pk).exists())
        self.assertEqual(JobSearchMessage.objects.count(), 2)
        self.assertEqual(
            set(JobSearchMessage.objects.values_list("conversation__owner", flat=True)),
            {other.pk},
        )
        self.assertEqual(
            list(UserPreference.objects.values_list("user", flat=True)), [other.pk]
        )
