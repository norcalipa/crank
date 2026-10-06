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
"""

import re
from pathlib import Path

import yaml
from django.test import SimpleTestCase, TestCase

from crank.models.monitoring import ALLOWED_CAPABILITY_KEYS
from crank.services import monitoring

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITORING_YAML = REPO_ROOT / "docs" / "monitoring.yaml"
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
}
NRQL_KEYS = {"event", "nrql", "operator", "threshold", "min_sample"}
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
                self.assertIn(gate["operator"], ("above", "below"))
                self.assertIs(type(gate["min_sample"]), int)
                self.assertGreaterEqual(gate["min_sample"], 1)

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

    def test_carried_over_numbers_match_the_yaml(self):
        rows = _section_table(
            self.section, "### Carried-over thresholds for #492 gates"
        )
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
        ):
            self.assertIn(phrase, normalized)


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
        rows = _table_rows(
            _section(USABILITY_DOC, "Decisions pending owner confirmation")
        )[1:]
        self.assertEqual([row[0] for row in rows], ["D1", "D2", "D3", "D4", "D5"])
        for row in rows:
            self.assertIn("default — owner may change", row[2])
        marker = "**default — owner may change:**"
        self.assertEqual(self.content.count(marker), 5)
        self.assertNotIn(marker, ROLLOUT_DOC.read_text(encoding="utf-8"))

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
