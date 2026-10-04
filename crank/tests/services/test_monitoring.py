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

    Event names and enum literals are checked in direct calls and in the view
    helpers that wrap them. Non-literal event names, payloads and values cannot
    be resolved statically, so each such site must appear in
    ``AUDITED_DYNAMIC`` keyed by file, enclosing function and key; a new
    dynamic site fails here until someone audits it, and a stale entry fails too.
    """

    # helper name -> (positional enum keys by index, keyword -> enum key)
    HELPERS = {
        "_turn_event": (
            ("phase",),
            {"phase": "phase", "reason_code": "reason_code",
             "failure_stage": "failure_stage", "latency_bucket": "latency_bucket"},
        ),
        "_turn_failed": (("reason_code",), {"reason_code": "reason_code"}),
        "_turn_rejected": (("reason_code",), {"reason_code": "reason_code"}),
        "_preference_decision_event": (
            (),
            {"decision": "decision", "scope": "scope", "ok_status": "status", "origin": "origin"},
        ),
    }
    # (file, enclosing function, key or <event name>/<payload>) -> (call-site
    # count, where the value comes from and what pins it to the registry). A new
    # dynamic value in a different function, key or call shape, or an extra call
    # site on a listed entry, fails the audit.
    AUDITED_DYNAMIC = {
        ("admin.py", "_state_action", "action"): (1, "state-action literals; test_monitoring_vendor_boundary"),
        ("admin.py", "_toggle", "capability"): (1, "CapabilitySwitch.key, registry holds ALLOWED_CAPABILITY_KEYS"),
        ("admin_dashboard.py", "_acquire_pipeline_slot", "action"): (1, "'Queue'.lower(); test_monitoring_vendor_boundary"),
        ("admin_dashboard.py", "_acquire_pipeline_slot", "reason_code"): (1, "skip_reason literals, registered; vendor-boundary test"),
        ("admin_dashboard.py", "_acquire_pipeline_slot", "run_type"): (1, "AgentRun.RunType member"),
        ("admin_dashboard.py", "retry_failed_view", "reason_code"): (1, "skip_reason literals, registered; vendor-boundary test"),
        ("admin_dashboard.py", "retry_failed_view", "run_type"): (1, "AgentRun.RunType member"),
        ("agent_runs.py", "claim_run", "run_type"): (1, "AgentRun.RunType value"),
        ("agent_runs.py", "finalize_failure", "reason_code"): (1, "failure_reason(), registered"),
        ("agent_runs.py", "finalize_failure", "run_type"): (1, "AgentRun.RunType value"),
        ("agent_runs.py", "finalize_failure", "status"): (1, "AgentRun.Status value / literal map"),
        ("agent_runs.py", "record_agent_event", "<payload>"): (1, "payload built from run fields; enum keys sanitized by event_attributes"),
        ("agent_runs.py", "record_skipped", "reason_code"): (1, "record_skipped reasons, registered"),
        ("agent_runs.py", "record_skipped", "run_type"): (1, "AgentRun.RunType value"),
        ("assistant_status.py", "assistant_status", "state"): (1, "assistant_status state constants"),
        ("crawl_healthcheck.py", "_emit_pipeline_health", "<payload>"): (1, "pipeline_health gauge dict; EVENT_SCHEMAS allowlist"),
        ("crawl_healthcheck.py", "handle", "<payload>"): (1, "healthcheck summary dict; sanitized by event_attributes"),
        ("crawl_healthcheck.py", "handle", "reason_code"): (1, "failure_reason()"),
        ("crawl_scheduler.py", "plan_crawls", "<payload>"): (2, "planning counts dict; sanitized by event_attributes"),
        ("job_ingest.py", "ingest_job_source", "reason_code"): (1, "SKIP_* constants"),
        ("job_matches.py", "job_match_status", "state"): (1, "empty_state constants"),
        ("job_pipeline.py", "run_job_pipeline", "<payload>"): (1, "stage summary dict; sanitized by event_attributes"),
        ("job_pipeline.py", "run_job_pipeline", "reason_code"): (2, "failure_reason() / literals"),
        ("job_search.py", "_preference_decision_event", "decision"): (1, "helper parameter; callers checked as literals"),
        ("job_search.py", "_preference_decision_event", "origin"): (1, "_token_origin() allowlist; test_assistant_telemetry origin tests"),
        ("job_search.py", "_preference_decision_event", "scope"): (1, "helper parameter; callers checked as literals"),
        ("job_search.py", "_preference_decision_event", "status"): (1, "derived from HTTP status: ok_status literals or stale/invalid/failed"),
        ("job_search.py", "_turn_event", "<payload>"): (1, "phase/attempt payload; phase resolved via HELPERS"),
        ("job_search.py", "_turn_event", "phase"): (1, "helper parameter; callers checked as literals"),
        ("job_search.py", "_turn_failed", "failure_stage"): (1, "failure_stage_for() output; test_failure_stage_for_table"),
        ("job_search.py", "_turn_failed", "reason_code"): (1, "helper parameter; callers checked as literals"),
        ("job_search.py", "_turn_interrupted", "failure_stage"): (1, "failure_stage_for() output; test_failure_stage_for_table"),
        ("job_search.py", "_turn_rejected", "failure_stage"): (1, "failure_stage_for() output; test_failure_stage_for_table"),
        ("job_search.py", "_turn_rejected", "reason_code"): (1, "helper parameter; callers checked as literals"),
        ("job_search.py", "agent_conversation_detail", "latency_bucket"): (1, "monitoring.latency_bucket()"),
        ("job_search.py", "agent_preference_apply", "decision"): (1, "meta['decision'] is apply or dismiss; apply tests"),
        ("job_search.py", "agent_preference_apply", "origin"): (1, "_token_origin() allowlist; test_assistant_telemetry origin tests"),
        ("job_search.py", "agent_preference_apply", "scope"): (1, "meta['scope'] is account or search; apply tests"),
        ("job_search.py", "agent_preference_undo", "origin"): (1, "_token_origin() allowlist; test_assistant_telemetry origin tests"),
        ("publication_sweep.py", "handle", "<payload>"): (1, "sweep summary dict; EVENT_SCHEMAS allowlist"),
        ("publication_sweep.py", "handle", "reason_code"): (1, "failure_reason()"),
        ("recompute_matches.py", "run_payload", "<payload>"): (1, "matching_batch counters dict; sanitized by event_attributes"),
        ("score_gathering.py", "gather_scores", "reason_code"): (1, "failure_reason()"),
        ("service.py", "_invoke_gateway", "reason_code"): (1, "failure_reason() / FailureCode"),
        ("service.py", "run", "availability_state"): (1, "availability dict state, registered"),
        ("service.py", "run", "latency_bucket"): (3, "monitoring.latency_bucket()"),
        ("service.py", "run", "reason_code"): (1, "failure_reason() / FailureCode"),
    }

    @staticmethod
    def _resolve(node, fn=None):
        """Literal string values ``node`` can take, or ``None`` if unresolvable.

        A bare name resolves through every assignment to it inside ``fn``.
        """
        import ast

        resolve = RecordEventRegistryTests._resolve
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            body, orelse = resolve(node.body, fn), resolve(node.orelse, fn)
            return body | orelse if body is not None and orelse is not None else None
        if isinstance(node, ast.Name) and fn is not None:
            values, found = set(), False
            for stmt in ast.walk(fn):
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == node.id for t in stmt.targets
                ):
                    found = True
                    resolved = resolve(stmt.value, fn)
                    if resolved is None:
                        return None
                    values |= resolved
            return values if found else None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "lower"
            and not node.args
        ):
            inner = resolve(node.func.value, fn)
            return {v.lower() for v in inner} if inner is not None else None
        return None

    def _walk(self):
        """Yield ``(file name, enclosing function name, call node, function node)``."""
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            tree = ast.parse(path.read_text())
            owner = {}
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for child in ast.walk(fn):
                        owner[child] = fn
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = owner.get(node)
                    yield path.name, fn.name if fn else "<module>", node, fn

    @classmethod
    def _dict_pairs(cls, node):
        """``(constant-key pairs, dynamic)`` for a dict literal, following
        ``**`` spreads of literal dicts (and ``{...} if c else {}`` forms)."""
        import ast

        if isinstance(node, ast.IfExp):
            body, body_dyn = cls._dict_pairs(node.body)
            orelse, orelse_dyn = cls._dict_pairs(node.orelse)
            return body + orelse, body_dyn or orelse_dyn
        if not isinstance(node, ast.Dict):
            return [], True
        pairs, dynamic = [], False
        for key, value in zip(node.keys, node.values):
            if key is None:
                sub, sub_dyn = cls._dict_pairs(value)
                pairs += sub
                dynamic = dynamic or sub_dyn
            elif isinstance(key, ast.Constant):
                pairs.append((key.value, value))
            else:
                dynamic = True
        return pairs, dynamic

    def _scan(self):
        """Classify every audited call.

        Returns ``(checked, names, dynamic)``: ``checked`` is a list of
        ``(site, key, literal)`` enum literals, ``names`` the literal event
        names, and ``dynamic`` a ``Counter`` of ``(file, function, what)``
        sites whose event name, payload or enum value cannot be resolved
        statically (one count per call site).
        """
        import ast
        from collections import Counter

        checked, names, dynamic = [], [], Counter()
        for fname, func, node, fn in self._walk():
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            pairs = []
            if name == "record_event":
                event = node.args[0] if node.args else kwargs.get("event_name")
                if event is None:
                    if "event_kind" not in kwargs:  # publication.record_event is unrelated
                        dynamic[(fname, func, "<call shape>")] += 1
                    continue
                payload = node.args[1] if len(node.args) > 1 else kwargs.get("attributes")
                resolved = self._resolve(event, fn)
                if resolved is None:
                    dynamic[(fname, func, "<event name>")] += 1
                else:
                    names.extend((f"{fname}:{node.lineno}", n) for n in resolved)
                if payload is not None:
                    pairs, payload_dynamic = self._dict_pairs(payload)
                    if payload_dynamic or not isinstance(payload, ast.Dict):
                        dynamic[(fname, func, "<payload>")] += 1
            elif name in self.HELPERS:
                positional, keyword = self.HELPERS[name]
                pairs = [(key, node.args[i]) for i, key in enumerate(positional) if len(node.args) > i]
                pairs += [(key, kwargs[kw]) for kw, key in keyword.items() if kw in kwargs]
            for key, value in pairs:
                if key not in monitoring._ENUM_KEYS:
                    continue
                values = self._resolve(value, fn)
                if values is None:
                    dynamic[(fname, func, key)] += 1
                    continue
                checked.extend((f"{fname}:{node.lineno}", key, v) for v in values)
        return checked, names, dynamic

    def test_enum_values_in_record_event_calls_are_registered(self):
        registry = monitoring.enum_values()
        checked, names, dynamic = self._scan()
        for site, literal in names:
            self.assertIn(literal, monitoring.EVENT_NAMES, f"{site} event name {literal!r}")
        for site, key, literal in checked:
            self.assertIn(literal, registry[key], f"{site} {key}={literal!r}")
        self.assertGreater(len(checked), 30)
        self.assertGreater(len(names), 30)
        pinned = {site: count for site, (count, _) in self.AUDITED_DYNAMIC.items()}
        self.assertEqual(
            dict(dynamic),
            pinned,
            "unresolvable record_event names/payloads/enum values need a vendor-boundary "
            "test and an AUDITED_DYNAMIC entry with the exact call-site count",
        )

    def test_crawl_run_skipped_is_registered_and_emitted(self):
        self.assertIn("crawl_run_skipped", monitoring.EVENT_NAMES)
        payload = monitoring.event_attributes("crawl_run_skipped", {"source_key": "fixture-adapter"})
        self.assertEqual(payload["event_name"], "crawl_run_skipped")

    def _scan_source(self, src):
        import ast
        import textwrap
        from unittest import mock

        tree = ast.parse(textwrap.dedent(src))
        calls = []
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                calls += [("fake.py", fn.name, n, fn) for n in ast.walk(fn) if isinstance(n, ast.Call)]
        with mock.patch.object(self, "_walk", return_value=calls):
            return self._scan()

    def _bad_literals(self, checked):
        registry = monitoring.enum_values()
        return {(key, lit) for _, key, lit in checked if lit not in registry[key]}

    def test_audit_flags_unregistered_names_and_unlisted_dynamic_sites(self):
        checked, names, dynamic = self._scan_source(
            """
            def emit(name, gauges):
                record_event("not_a_real_event", {"status": "ok"})
                record_event(name, gauges)
                record_event("assistant_turn", {"reason_code": gauges})
            """
        )
        self.assertIn(("fake.py:3", "not_a_real_event"), names)
        self.assertNotIn("not_a_real_event", monitoring.EVENT_NAMES)
        self.assertEqual(
            dict(dynamic),
            {
                ("fake.py", "emit", "<event name>"): 1,
                ("fake.py", "emit", "<payload>"): 1,
                ("fake.py", "emit", "reason_code"): 1,
            },
        )
        self.assertFalse(set(dynamic) & set(self.AUDITED_DYNAMIC))

    def test_audit_checks_literals_inside_spread_payloads(self):
        checked, _, dynamic = self._scan_source(
            """
            def emit(counts, ok):
                record_event("matching_batch", {**counts, "failure_stage": "matchng"})
                record_event("matching_batch", {**counts, **({"failure_stage": "matchng"} if ok else {})})
                record_event("publication_sweep", {**counts, "status": "bogus_status"})
                record_event("matching_batch", {**counts, "stage": "job_pipeline_matchng"})
            """
        )
        self.assertEqual(
            self._bad_literals(checked),
            {("failure_stage", "matchng"), ("status", "bogus_status"), ("stage", "job_pipeline_matchng")},
        )
        self.assertEqual(dynamic[("fake.py", "emit", "<payload>")], 4)

    def test_audit_checks_helper_keyword_arguments(self):
        checked, _, dynamic = self._scan_source(
            """
            def emit(response):
                _turn_event("failed", reason_code="worker_interuptd", failure_stage="matchng")
                _turn_failed(reason_code="provider_timeuot")
                _turn_event(phase="replyed", latency_bucket="slowish")
                _preference_decision_event(
                    response, decision="apply", scope="search", ok_status="applied",
                    origin="priorities_editor",
                )
            """
        )
        self.assertEqual(
            self._bad_literals(checked),
            {
                ("reason_code", "worker_interuptd"),
                ("failure_stage", "matchng"),
                ("reason_code", "provider_timeuot"),
                ("phase", "replyed"),
                ("latency_bucket", "slowish"),
                ("origin", "priorities_editor"),
            },
        )
        self.assertFalse(dynamic)

    def test_audit_handles_keyword_form_record_event(self):
        checked, names, dynamic = self._scan_source(
            """
            def emit(payload):
                monitoring.record_event(event_name="bogus_evt", attributes={"status": "bogus_status"})
                monitoring.record_event(attributes=payload)
                publication.record_event(target_type="t", target_id=1, event_kind="k")
            """
        )
        self.assertIn(("fake.py:3", "bogus_evt"), names)
        self.assertEqual(self._bad_literals(checked), {("status", "bogus_status")})
        self.assertEqual(dict(dynamic), {("fake.py", "emit", "<call shape>"): 1})

    def test_audit_resolves_event_name_variables_in_their_function(self):
        _, names, dynamic = self._scan_source(
            """
            def emit(failed, other):
                event = "crawl_run_completed" if failed else "crawl_run_skipped"
                if other:
                    event = "crawl_run_skiped"
                record_event(event, {})

            def opaque(other):
                event = "crawl_run_failed"
                event = other.name
                record_event(event, {})
            """
        )
        self.assertEqual(
            sorted(n for _, n in names),
            ["crawl_run_completed", "crawl_run_skiped", "crawl_run_skipped"],
        )
        self.assertEqual(dict(dynamic), {("fake.py", "opaque", "<event name>"): 1})

    def test_audit_pins_call_site_counts_per_entry(self):
        _, _, dynamic = self._scan_source(
            """
            def emit(a, b):
                record_event("assistant_turn", a)
                record_event("assistant_turn", b)
            """
        )
        self.assertEqual(dynamic[("fake.py", "emit", "<payload>")], 2)
        key = ("agent_runs.py", "record_agent_event", "<payload>")
        self.assertNotEqual(self.AUDITED_DYNAMIC[key][0], 2)

    def test_computed_sources_stay_inside_the_registry(self):
        registry = monitoring.enum_values()
        for exc in (None, TimeoutError(), ValueError(), PermissionError(), ConnectionError()):
            self.assertIn(monitoring.failure_reason(exc), registry["reason_code"])
        for ms in (0, 99, 100, 299, 300, 999, 1000, 10**6):
            self.assertIn(monitoring.latency_bucket(ms), registry["latency_bucket"])
        self.assertTrue({"approve", "block", "queue"} <= registry["action"])
