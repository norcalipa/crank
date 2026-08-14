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
                "stage": "completion",
            },
        )
        self.assertEqual(payload["event_name"], "interactive_call")
        self.assertEqual(payload["reason_code"], "timeout")
        self.assertNotIn("prompt", payload)
        self.assertNotIn("response_body", payload)
        self.assertNotIn("user_id", payload)
        self.assertEqual(payload["stage"], "completion")

    def test_event_schema_rejects_unknown_event_names(self):
        with self.assertRaises(ValueError):
            monitoring.event_attributes("raw_prompt", {})

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
        result = monitoring._safe_value("stage", "a" * 200)
        self.assertEqual(len(result), 64)

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
                "latency_bucket": monitoring.latency_bucket(250),
                "latency_ms": 250,
                "provider_error_class": "ProviderTimeoutError",
                "turns_without_result": 3,
            },
        )
        self.assertEqual(payload["event_name"], "job_search_turn")
        self.assertEqual(payload["tools_called"], 4)
        self.assertTrue(payload["empty_result"])
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
