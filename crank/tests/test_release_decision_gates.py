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
rules written in ``docs/monitoring.md``. ``_outcome`` below is those rules as
code, so the tests can show that no gate passes while its sample floor is
unset and that every gate can reach pass, hold and breach once it is locked.
"""

import re
from pathlib import Path

import yaml
from django.test import SimpleTestCase, TestCase

from crank.models.monitoring import ALLOWED_CAPABILITY_KEYS
from crank.services import monitoring

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
    "assistant-ready-share",
    "job-source-alerts-quiet",
    "job-matches-source-unavailable",
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
QUIET_KEYS = {"alerts", "signal_event", "policy_confirmed"}
FROM_CLAUSE = " FROM CrankOperation "
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


def _split_query(nrql: str) -> tuple:
    """``(select expression, text from FROM onward)`` of a gate query."""
    select, tail = nrql.split(FROM_CLAUSE, 1)
    assert select.startswith("SELECT "), nrql
    return select[len("SELECT "):], tail


def _is_ratio(gate: dict) -> bool:
    return gate["kind"] == "nrql" and " / " in _split_query(gate["nrql"])[0]


def _outcome(gate: dict, sample, value) -> str:
    """The documented evaluation rules (docs/monitoring.md), in order."""
    if gate["min_sample"] is None:
        return "hold"
    if sample is None or sample < gate["min_sample"]:
        return "hold"
    if gate["kind"] == "alerts_quiet" and gate["policy_confirmed"] is not True:
        return "hold"
    if gate["threshold"] is None or value is None:
        return "hold"
    if gate["operator"] == "above":
        return "breach" if value > gate["threshold"] else "pass"
    return "breach" if value < gate["threshold"] else "pass"


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
        window as the sample, and stays unconfirmed until the owner checks the
        alert policy exists (the repository wires none).
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
                self.assertEqual(
                    gate["sample_nrql"],
                    f"SELECT count(*){FROM_CLAUSE}WHERE event_name = '{signal}'"
                    f" SINCE {gate['window']} ago",
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

    def test_monitoring_doc_states_the_evaluation_rules(self):
        text = " ".join(MONITORING_DOC.read_text(encoding="utf-8").split())
        for phrase in (
            "#### Release decision gates",
            "`min_sample: null` — **hold**",
            "below `min_sample` — **hold**",
            "`policy_confirmed: false` — **hold**",
            "`threshold: null` — **hold**",
            "the denominator of the ratio",
            "the number of `signal_event` events",
            "No gate can pass as checked in",
        ):
            self.assertIn(phrase, text)


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
        switch_by_phase = {
            gate["phase"]: gate["switch"] for gate in _gates()
        }
        for row in phases:
            phase = row[0].strip("`")
            if phase == "shell":
                self.assertTrue(row[2].startswith("none"))
                continue
            documented = row[2].split("`")[1]
            self.assertIn(documented, ALLOWED_CAPABILITY_KEYS)
            if phase in switch_by_phase:
                self.assertEqual(documented, switch_by_phase[phase])

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
        ):
            self.assertIn(phrase, normalized)

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
    """Body rows of the first table under a ``###`` heading inside a section."""
    lines = section.splitlines()
    start = lines.index(heading)
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("### ")
        ),
        len(lines),
    )
    return _table_rows("\n".join(lines[start:end]))[1:]


class RunbookDurableEnablementTests(SimpleTestCase):
    """The crawl runbooks only instruct steps the deploy workflows keep."""

    RUNBOOKS = ("runbook-initial-crawl.md", "runbook-crawl-scheduling.md")
    REAPPLIED = {
        "crank-crawl-cron.yaml": "crank-crawl-organizations",
        "crank-healthcheck-cron.yaml": "crank-healthcheck",
    }

    def _text(self, name: str) -> str:
        return (REPO_ROOT / "docs" / name).read_text(encoding="utf-8")

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
                self.assertNotIn("kubectl -n crank apply -f k8s/", text)
                for cronjob in self.REAPPLIED.values():
                    self.assertNotIn(f"patch cronjob {cronjob}", text)
                self.assertIn("patch cronjob crank-job-pipeline", text)
                self.assertIn("deploy/cronjob-job-pipeline.yaml", text)

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
                ):
                    self.assertIn(phrase, normalized)
        initial = " ".join(self._text(self.RUNBOOKS[0]).split())
        self.assertIn("k8s/crank-healthcheck-cron.yaml", initial)
        self.assertIn("revert the commit", initial)


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
        self.assertEqual([row[0] for row in tasks], ["T1", "T2", "T3", "T4"])
        for row in tasks:
            self.assertTrue(row[1] and row[2], row[0])
        probes = _table_rows(_section(USABILITY_DOC, "Comprehension probes"))[1:]
        self.assertEqual(
            [row[0].split(" ")[0] for row in probes], ["C1", "C2", "C3", "C4"]
        )
        for row in probes:
            self.assertTrue(row[1] and row[2], row[0])
        self.assertIn("T1 to T3, in order, are the **core task**", self.normalized)

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
        for item in (
            "names",
            "e-mail addresses",
            "employers",
            "real compensation figures",
            "real preference values",
            "account identifiers",
            "prompts and replies verbatim",
            "screen recordings stored in the repository",
        ):
            self.assertIn(f"- {item}", rules)
        self.assertIn("recorded as P1–P5", rules)
        self.assertIn("Retention limit for raw notes", rules)

    def test_participants_cover_fresh_and_returning(self):
        section = " ".join(
            _section(USABILITY_DOC, "Participants and recruiting criteria").split()
        )
        self.assertIn("Five participants", section)
        self.assertIn("**fresh**", section)
        self.assertIn("**returning**", section)

    def test_five_decisions_are_marked_as_defaults_in_one_place(self):
        section = _section(USABILITY_DOC, "Decisions pending owner confirmation")
        rows = _table_rows(section.split(FLOORS_HEADING)[0])[1:]
        self.assertEqual([row[0] for row in rows], ["D1", "D2", "D3", "D4", "D5"])
        for row in rows:
            self.assertIn("default — owner may change", row[2])
        marker = "**default — owner may change:**"
        self.assertEqual(self.content.count(marker), 5)
        self.assertNotIn(marker, ROLLOUT_DOC.read_text(encoding="utf-8"))

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
                    self.assertEqual(row[4], "not confirmed")
                else:
                    self.assertIn(f"`{gate['event']}`", row[1])
                    self.assertEqual(row[4], "—")

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
            self.assertTrue(row[2].startswith("yes"), row[0])
            self.assertTrue(row[3].startswith("yes"), row[0])
            self.assertTrue(_CITATION.findall(row[4]), row[0])

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
