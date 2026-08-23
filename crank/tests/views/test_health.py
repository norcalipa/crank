# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from unittest.mock import Mock, patch

from django.test import Client, SimpleTestCase, override_settings


class ReadinessTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    @patch("crank.views.health.MigrationExecutor")
    def test_ready_when_no_migrations_and_capabilities_ok(self, executor_class):
        executor = executor_class.return_value
        executor.loader.graph.leaf_nodes.return_value = [("crank", "0018")]
        executor.migration_plan.return_value = []

        response = self.client.get("/healthz/ready/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["pending_migrations"], 0)
        self.assertIn("capabilities", body)
        self.assertTrue(body["capabilities"]["all_ok"])

    @patch("crank.views.health.MigrationExecutor")
    def test_not_ready_when_migrations_are_pending(self, executor_class):
        executor = executor_class.return_value
        executor.loader.graph.leaf_nodes.return_value = [("crank", "0018")]
        executor.migration_plan.return_value = [Mock(), Mock()]

        response = self.client.get("/healthz/ready/")

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["pending_migrations"], 2)
        self.assertIn("capabilities", body)

    @patch("crank.views.health.MigrationExecutor", side_effect=RuntimeError("DB down"))
    def test_not_ready_when_database_is_unreachable(self, executor_class):
        response = self.client.get("/healthz/ready/")

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "unavailable")
        self.assertIsNone(body["pending_migrations"])
        self.assertIsNone(body["capabilities"])

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="",
        LLM_MODEL="",
        LLM_API_KEY="",
    )
    @patch("crank.views.health.MigrationExecutor")
    def test_not_ready_when_capability_misconfigured(self, executor_class):
        executor = executor_class.return_value
        executor.loader.graph.leaf_nodes.return_value = [("crank", "0018")]
        executor.migration_plan.return_value = []

        response = self.client.get("/healthz/ready/")

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["pending_migrations"], 0)
        self.assertIn("capabilities", body)
        self.assertFalse(body["capabilities"]["all_ok"])
        issues = body["capabilities"]["capability_issues"]
        # issues is a list of "name: issue" strings
        issue_text = " ".join(issues)
        self.assertIn("LLM_PROVIDER", issue_text)

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="crank.agents.llm:FakeLLMProvider",
        LLM_MODEL="test-model",
        LLM_API_KEY="test-key",
    )
    @patch("crank.views.health.MigrationExecutor")
    def test_ready_when_capability_configured(self, executor_class):
        executor = executor_class.return_value
        executor.loader.graph.leaf_nodes.return_value = [("crank", "0018")]
        executor.migration_plan.return_value = []

        response = self.client.get("/healthz/ready/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ready")
        self.assertTrue(body["capabilities"]["all_ok"])

    @override_settings(
        INTERACTIVE_AGENT_ENABLED=True,
        LLM_PROVIDER="crank.agents.llm:FakeLLMProvider",
        LLM_MODEL="test",
        LLM_API_KEY="sk-super-secret-key",
    )
    @patch("crank.views.health.MigrationExecutor")
    def test_ready_response_never_leaks_secrets(self, executor_class):
        executor = executor_class.return_value
        executor.loader.graph.leaf_nodes.return_value = [("crank", "0018")]
        executor.migration_plan.return_value = []

        response = self.client.get("/healthz/ready/")
        import json

        blob = json.dumps(response.json())
        self.assertNotIn("sk-super-secret-key", blob)
        self.assertNotIn("api_key", blob.lower())
        self.assertNotIn("secret", blob.lower())
