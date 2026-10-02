# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from crank.models import AgentRun, CapabilitySwitch, OperationalChangeAudit
from crank.services import agent_runs, monitoring


class MonitoringContractTests(TestCase):
    def test_event_schema_is_allowlisted_and_redacts_sensitive_attributes(self):
        payload = monitoring.event_attributes(
            "interactive_call",
            {
                "status": "failed",
                "reason_code": "timeout",
                "prompt": "private user request",
                "response_body": "private provider body",
                "user_id": "must-not-be-a-dimension",
                "stage": "job_ingest",
            },
        )
        self.assertEqual(payload["event_name"], "interactive_call")
        self.assertEqual(payload["reason_code"], "timeout")
        self.assertNotIn("prompt", payload)
        self.assertNotIn("response_body", payload)
        self.assertNotIn("user_id", payload)
        self.assertEqual(payload["stage"], "job_ingest")

    def test_event_schema_rejects_unknown_event_names(self):
        with self.assertRaises(ValueError):
            monitoring.event_attributes("raw_prompt", {})

    def test_match_recompute_safe_keys_pass_through(self):
        """issue #475: stale_discarded/duplicate_skipped are safe counters on
        the matching_batch event."""
        payload = monitoring.event_attributes(
            "matching_batch",
            {
                "stage": "match_recompute",
                "status": "completed",
                "users_total": 3,
                "stale_discarded": 1,
                "duplicate_skipped": 2,
            },
        )
        self.assertEqual(payload["stale_discarded"], 1)
        self.assertEqual(payload["duplicate_skipped"], 2)

    def test_inventory_health_payload_excludes_violations(self):
        # Operator-only detail must never leak into the bounded telemetry event;
        # guards a future regression if "violations" were allowlisted.
        payload = monitoring.event_attributes(
            "inventory_health",
            {
                "healthy": False,
                "enabled_sources": 0,
                "violations": ["no approved and enabled job sources"],
            },
        )
        self.assertNotIn("violations", payload)
        self.assertFalse(payload["healthy"])
        self.assertEqual(payload["enabled_sources"], 0)

    def test_reason_codes_are_finite_and_stable(self):
        self.assertEqual(monitoring.failure_reason(TimeoutError()), "timeout")
        self.assertEqual(
            monitoring.failure_reason(type("InvalidModelOutputError", (Exception,), {})()),
            "rejected",
        )
        self.assertEqual(
            monitoring.failure_reason(type("CostLimitError", (Exception,), {})()),
            "cost_limit",
        )
        self.assertEqual(monitoring.failure_reason(PermissionError()), "authorization")
        self.assertEqual(monitoring.failure_reason(ConnectionError()), "upstream")
        self.assertEqual(monitoring.failure_reason(None), "none")

    @patch("crank.services.monitoring.newrelic.agent.record_custom_event")
    def test_record_event_mocks_new_relic_and_never_sends_content(self, record):
        monitoring.record_event(
            "source_stage",
            {"source_key": "adapter.v1", "content": "raw source body"},
        )
        record.assert_called_once()
        event_type, payload = record.call_args.args
        self.assertEqual(event_type, "CrankOperation")
        self.assertEqual(payload["source_key"], "adapter.v1")
        self.assertNotIn("content", payload)

    @patch("crank.services.monitoring.newrelic.agent.record_custom_metric")
    def test_record_metric_is_best_effort(self, record):
        monitoring.record_metric("Crank/Test/LatencyMs", 12.5)
        record.assert_called_once_with("Crank/Test/LatencyMs", 12.5)

    @patch("crank.services.agent_runs.monitoring.record_event")
    @patch("crank.services.agent_runs.newrelic.agent.record_custom_event")
    def test_agent_event_only_contains_scalar_allowlisted_counts(
        self, record, operation_event
    ):
        run = AgentRun.objects.create(run_type=AgentRun.RunType.NOOP)
        agent_runs.record_agent_event(
            run,
            "run_started",
            counts={"items_seen": 2, "raw_body": "never", "unknown": 99},
            started_at="not a dimension",
        )
        payload = record.call_args.args[1]
        self.assertEqual(payload["counts"], {"items_seen": 2})
        self.assertNotIn("raw_body", payload["counts"])
        self.assertNotIn("unknown", payload["counts"])
        self.assertNotIn("started_at", payload)
        operation_event.assert_not_called()

    def test_capability_switch_defaults_and_override(self):
        self.assertTrue(monitoring.capability_enabled("missing"))
        switch = CapabilitySwitch.objects.create(key="job_pipeline", enabled=False)
        self.assertFalse(monitoring.capability_enabled("job_pipeline"))
        self.assertEqual(str(switch), "job_pipeline [off]")
        with self.assertRaises(ValidationError):
            CapabilitySwitch(key="unregistered", enabled=True).full_clean()

    def test_audit_values_are_bounded_and_redacted(self):
        audit = OperationalChangeAudit.record(
            actor=None,
            target_type="capability",
            target_id="job_pipeline",
            action="changed",
            old_value={"prompt": "private", "nested": ["x"]},
            new_value={"enabled": False},
        )
        self.assertEqual(audit.old_value["prompt"], "<redacted>")
        self.assertEqual(audit.old_value["nested"], ["x"])

    def test_safe_value_returns_none_for_none(self):
        # The _coerce/_safe_value path must return None when value is None
        # (covers the value is None branch).
        self.assertIsNone(monitoring._safe_value("status", None))

    def test_safe_value_truncates_strings(self):
        self.assertEqual(monitoring._safe_value("stage", "a" * 200), "other")
        self.assertEqual(monitoring._safe_value("source_key", "a" * 200), "other")

    def test_audit_str_representation(self):
        audit = OperationalChangeAudit.record(
            actor=None,
            target_type="capability",
            target_id="job_pipeline",
            action="changed",
        )
        self.assertEqual(str(audit), "changed:capability:job_pipeline")

    def test_latency_buckets_are_low_cardinality(self):
        self.assertEqual(monitoring.latency_bucket(50), "lt100")
        self.assertEqual(monitoring.latency_bucket(100), "100-300")
        self.assertEqual(monitoring.latency_bucket(299), "100-300")
        self.assertEqual(monitoring.latency_bucket(300), "300-1000")
        self.assertEqual(monitoring.latency_bucket(999), "300-1000")
        self.assertEqual(monitoring.latency_bucket(5000), "gt1000")

    def test_job_search_turn_event_accepts_quality_dimensions(self):
        payload = monitoring.event_attributes(
            "job_search_turn",
            {
                "tools_called": 4,
                "result_count": 12,
                "cited_ids_count": 0,
                "empty_result": True,
                "inventory_nonempty": True,
                "page_context": "fresh",
                "latency_bucket": monitoring.latency_bucket(250),
                "latency_ms": 250,
                "provider_error_class": "ProviderTimeoutError",
                "turns_without_result": 3,
            },
        )
        self.assertEqual(payload["event_name"], "job_search_turn")
        self.assertEqual(payload["tools_called"], 4)
        self.assertTrue(payload["empty_result"])
        self.assertEqual(payload["page_context"], "fresh")
        self.assertEqual(payload["latency_bucket"], "100-300")

    def test_job_search_tool_invocation_is_registered(self):
        payload = monitoring.event_attributes(
            "job_search_tool_invocation",
            {"tool": "search_job_listings", "result_count": 3, "job_match_count": 1, "organization_match_count": 1},
        )
        self.assertEqual(payload["tool"], "search_job_listings")
        self.assertEqual(payload["result_count"], 3)

    def test_helpfulness_gap_event_is_registered(self):
        payload = monitoring.event_attributes(
            "job_search_helpfulness_gap",
            {"turns_without_result": 5, "empty_result": True},
        )
        self.assertEqual(payload["event_name"], "job_search_helpfulness_gap")
        self.assertEqual(payload["turns_without_result"], 5)

    def test_monitoring_yaml_allowlist_matches_code_event_names(self):
        """MINOR-3: The YAML allowed_event_names must match EVENT_NAMES in code.

        Parses ``docs/monitoring.yaml`` and diffs its ``allowed_event_names``
        list against ``crank.services.monitoring.EVENT_NAMES`` so future drift
        between the two is detected at test time.
        """
        import pathlib
        import yaml

        yaml_path = pathlib.Path(__file__).resolve().parents[3] / "docs" / "monitoring.yaml"
        with open(yaml_path) as fh:
            doc = yaml.safe_load(fh)
        yaml_names = frozenset(doc["allowed_event_names"])
        self.assertEqual(
            yaml_names,
            monitoring.EVENT_NAMES,
            msg="monitoring.yaml allowed_event_names drifted from EVENT_NAMES",
        )

    def test_recurring_helpfulness_gaps_alert_has_operator(self):
        """MINOR-2: the recurring-helpfulness-gaps alert must specify an operator.

        Scoped to the specific alert that lacked one (other alerts deliberately
        omit ``operator``, so a cross-alert assertion would be wrong).
        """
        import pathlib
        import yaml

        yaml_path = pathlib.Path(__file__).resolve().parents[3] / "docs" / "monitoring.yaml"
        with open(yaml_path) as fh:
            doc = yaml.safe_load(fh)
        alert = next(a for a in doc["alerts"] if a["name"] == "recurring-helpfulness-gaps")
        self.assertEqual(alert.get("operator"), "above")


class ListingLifecycleAttributeTests(TestCase):
    def test_listing_lifecycle_attributes_are_allowlisted(self):
        payload = monitoring.event_attributes(
            "source_stage",
            {
                "stage": "job_ingest",
                "source_key": "usajobs",
                "status": "succeeded",
                "listings_closed": 3,
                "listings_expired": 1,
                "listings_deleted": 2,
                "reason_code": "none",
            },
        )
        self.assertEqual(payload["listings_closed"], 3)
        self.assertEqual(payload["listings_expired"], 1)
        self.assertEqual(payload["listings_deleted"], 2)


HOSTILE_VALUES = (
    "https://x/y",
    "a@b.c",
    "remote jobs in sf",
    "q" * 1000,
    '{"patch": {"remote": true}}',
    "user:42",
)


class AssistantTelemetryPolicyTests(TestCase):
    def test_enum_keys_map_hostile_values_to_other(self):
        for key in sorted(monitoring._ENUM_KEYS):
            for value in HOSTILE_VALUES:
                self.assertEqual(monitoring._safe_value(key, value), "other", (key, value))
            self.assertEqual(monitoring._safe_value(key, 7), "other")

    def test_registered_enum_value_passes(self):
        self.assertEqual(monitoring._safe_value("phase", "replied"), "replied")
        self.assertEqual(monitoring._safe_value("state", "ready"), "ready")

    def test_slug_keys_other_for_non_slug(self):
        self.assertEqual(monitoring._safe_value("source_key", "usajobs"), "usajobs")
        for value in HOSTILE_VALUES:
            if value == "user:42":
                continue  # slug-shaped identifiers (provider:model) are allowed
            self.assertEqual(monitoring._safe_value("source_key", value), "other")

    def test_action_drop_reasons_slugged_and_bounded(self):
        self.assertEqual(monitoring._safe_value("action_drop_reasons", "a,b"), "a,b")
        self.assertEqual(monitoring._safe_value("action_drop_reasons", ""), "")
        self.assertEqual(monitoring._safe_value("action_drop_reasons", "x y,https://a"), "other,other")

    def test_correlation_id_must_be_uuid(self):
        good = "0f8fad5bd9cb469fa16570867728950e"
        self.assertEqual(monitoring._safe_value("correlation_id", good), good)
        canonical = "0f8fad5b-d9cb-469f-a165-70867728950e"
        self.assertEqual(monitoring._safe_value("correlation_id", canonical), canonical)
        for value in HOSTILE_VALUES:
            self.assertIsNone(monitoring._safe_value("correlation_id", value))

    def test_run_id_must_be_int(self):
        self.assertEqual(monitoring._safe_value("run_id", 5), 5)
        self.assertIsNone(monitoring._safe_value("run_id", True))
        self.assertIsNone(monitoring._safe_value("run_id", "5"))

    def test_counters_keep_numbers_and_drop_strings(self):
        self.assertEqual(monitoring._safe_value("results", 3), 3)
        self.assertIs(monitoring._safe_value("cached", True), True)
        self.assertEqual(monitoring._safe_value("seconds_to_first_result", 1.5), 1.5)
        self.assertIsNone(monitoring._safe_value("results", "remote jobs in sf"))

    def test_unknown_and_sensitive_keys_are_dropped(self):
        self.assertIsNone(monitoring._safe_value("message", "hi"))
        self.assertIsNone(monitoring._safe_value("response_body", 1))

    def test_assistant_turn_payload_contains_no_hostile_value(self):
        payload = monitoring.event_attributes(
            "assistant_turn",
            {
                "phase": "https://x/y",
                "reason_code": "a@b.c",
                "failure_stage": "user:42",
                "correlation_id": "remote jobs in sf",
                "message": "q" * 1000,
                "results": "q" * 1000,
                "turns": 2,
            },
        )
        self.assertEqual(
            payload,
            {"event_name": "assistant_turn", "phase": "other", "reason_code": "other",
             "failure_stage": "other", "turns": 2},
        )

    def test_per_event_schema_drops_keys_outside_schema(self):
        payload = monitoring.event_attributes(
            "assistant_first_result",
            {"seconds_to_first_result": 4, "turns_to_first_result": 1, "phase": "replied"},
        )
        self.assertEqual(
            payload,
            {"event_name": "assistant_first_result", "seconds_to_first_result": 4,
             "turns_to_first_result": 1},
        )

    def test_failure_stage_for_table(self):
        for reason, stage in monitoring._FAILURE_STAGE_BY_REASON.items():
            self.assertEqual(monitoring.failure_stage_for(reason), stage)
            self.assertIn(stage, monitoring.FAILURE_STAGES)
        self.assertEqual(monitoring.failure_stage_for("never-seen"), "internal")
        self.assertEqual(monitoring.failure_stage_for(None), "internal")

    def test_enum_registry_covers_dynamic_sources(self):
        from crank.agents.jobs import ingest
        from crank.models.job_search import JobSearchTurn

        registry = monitoring.enum_values()
        self.assertTrue(set(AgentRun.RunType.values) <= registry["run_type"])
        self.assertTrue(set(AgentRun.Status.values) <= registry["status"])
        self.assertTrue(set(JobSearchTurn.FailureCode.values) <= registry["reason_code"])
        skips = {getattr(ingest, n) for n in dir(ingest) if n.startswith("SKIP_")}
        self.assertTrue(skips <= registry["reason_code"])
        self.assertTrue(set(monitoring.FAILURE_STAGES) == registry["failure_stage"])
        for reason in monitoring._FAILURE_STAGE_BY_REASON:
            self.assertIn(reason, registry["reason_code"], reason)

    def test_yaml_metrics_block_shape(self):
        import pathlib
        import yaml

        doc = yaml.safe_load(
            (pathlib.Path(__file__).resolve().parents[3] / "docs" / "monitoring.yaml").read_text()
        )
        facets = set(doc["facets"])
        self.assertTrue(doc["metrics"])
        for metric in doc["metrics"]:
            self.assertIn(metric["event"], monitoring.EVENT_NAMES)
            self.assertTrue(set(metric["dimensions"]) <= facets, metric["name"])
            self.assertEqual(metric["owner"], "maintainer (crank.fyi)")
            self.assertIs(metric["baseline_only"], True)
            path, _, anchor = metric["runbook"].partition("#")
            root = pathlib.Path(__file__).resolve().parents[3]
            text = (root / path).read_text()
            self.assertIn(anchor, [
                line.lstrip("# ").lower().replace(" ", "-")
                for line in text.splitlines() if line.startswith("#")
            ])


class RecordEventRegistryTests(TestCase):
    """Every enum value reaching ``record_event`` is registered or audited.

    Literals are checked in direct calls and in the view helpers that wrap
    them. Non-literal values cannot be resolved statically, so each such site
    must appear in ``AUDITED_DYNAMIC`` with the test that drives it at the
    vendor boundary; a new dynamic site fails here until someone audits it.
    """

    HELPERS = {
        "_turn_event": {"phase": 0},
        "_turn_failed": {"reason_code": 0},
        "_turn_rejected": {"reason_code": 0},
    }
    HELPER_KWARGS = {"_preference_decision_event": {"decision", "scope", "ok_status"}}
    # site -> where the value comes from and what pins it to the registry
    AUDITED_DYNAMIC = {
        ("admin.py", "action"): "state-action literals; test_monitoring_vendor_boundary",
        ("admin.py", "capability"): "CapabilitySwitch.key, registry holds ALLOWED_CAPABILITY_KEYS",
        ("admin_dashboard.py", "action"): "'Queue'.lower(); test_monitoring_vendor_boundary",
        ("admin_dashboard.py", "reason_code"): "skip_reason literals, registered; vendor-boundary test",
        ("admin_dashboard.py", "run_type"): "AgentRun.RunType member",
        ("agent_runs.py", "run_type"): "AgentRun.RunType value",
        ("agent_runs.py", "status"): "AgentRun.Status value / literal map",
        ("agent_runs.py", "reason_code"): "failure_reason() or record_skipped reasons, registered",
        ("assistant_status.py", "state"): "assistant_status state constants",
        ("job_matches.py", "state"): "empty_state constants",
        ("crawl_healthcheck.py", "reason_code"): "failure_reason()",
        ("publication_sweep.py", "reason_code"): "failure_reason()",
        ("job_search.py", "origin"): "_token_origin() allowlist; test_assistant_telemetry origin tests",
        ("job_ingest.py", "reason_code"): "SKIP_* constants",
        ("job_pipeline.py", "reason_code"): "failure_reason() / literals",
        ("score_gathering.py", "reason_code"): "failure_reason()",
        ("job_search.py", "phase"): "helper parameter; callers checked as literals",
        ("job_search.py", "status"): "helper parameter; callers checked as literals",
        ("job_search.py", "decision"): "helper parameter; callers checked as literals",
        ("job_search.py", "scope"): "helper parameter; callers checked as literals",
        ("service.py", "availability_state"): "availability dict state, registered",
        ("service.py", "latency_bucket"): "monitoring.latency_bucket()",
        ("service.py", "reason_code"): "failure_reason() / FailureCode",
    }

    @staticmethod
    def _resolve(node):
        import ast

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            body, orelse = (RecordEventRegistryTests._resolve(n) for n in (node.body, node.orelse))
            return body | orelse if body is not None and orelse is not None else None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "lower"
            and not node.args
        ):
            inner = RecordEventRegistryTests._resolve(node.func.value)
            return {v.lower() for v in inner} if inner is not None else None
        return None

    def _walk(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call):
                    yield path, node

    def test_enum_values_in_record_event_calls_are_registered(self):
        import ast

        registry = monitoring.enum_values()
        checked = 0
        dynamic = set()
        for path, node in self._walk():
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            pairs = []
            if name == "record_event" and len(node.args) >= 2 and isinstance(node.args[1], ast.Dict):
                pairs = [
                    (k.value, v)
                    for k, v in zip(node.args[1].keys, node.args[1].values)
                    if isinstance(k, ast.Constant)
                ]
            elif name in self.HELPERS:
                pairs = [
                    (key, node.args[idx])
                    for key, idx in self.HELPERS[name].items()
                    if len(node.args) > idx
                ]
            elif name in self.HELPER_KWARGS:
                pairs = [
                    ({"ok_status": "status"}.get(kw.arg, kw.arg), kw.value)
                    for kw in node.keywords
                    if kw.arg in self.HELPER_KWARGS[name]
                ]
            for key, value in pairs:
                if key not in monitoring._ENUM_KEYS:
                    continue
                values = self._resolve(value)
                if values is None:
                    dynamic.add((path.name, key))
                    continue
                for literal in values:
                    checked += 1
                    self.assertIn(
                        literal, registry[key], f"{path.name}:{node.lineno} {key}={literal!r}"
                    )
        self.assertGreater(checked, 30)
        unaudited = {site for site in dynamic if site not in self.AUDITED_DYNAMIC}
        self.assertFalse(
            unaudited,
            f"dynamic enum values need a vendor-boundary test and an AUDITED_DYNAMIC entry: {sorted(unaudited)}",
        )

    def test_computed_sources_stay_inside_the_registry(self):
        registry = monitoring.enum_values()
        for exc in (None, TimeoutError(), ValueError(), PermissionError(), ConnectionError()):
            self.assertIn(monitoring.failure_reason(exc), registry["reason_code"])
        for ms in (0, 99, 100, 299, 300, 999, 1000, 10**6):
            self.assertIn(monitoring.latency_bucket(ms), registry["latency_bucket"])
        self.assertTrue({"approve", "block", "queue"} <= registry["action"])
