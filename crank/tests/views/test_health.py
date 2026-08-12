# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from unittest.mock import Mock, patch

from django.test import Client, SimpleTestCase


class ReadinessTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    @patch("crank.views.health.MigrationExecutor")
    def test_ready_when_no_migrations_are_pending(self, executor_class):
        executor = executor_class.return_value
        executor.loader.graph.leaf_nodes.return_value = [("crank", "0018")]
        executor.migration_plan.return_value = []

        response = self.client.get("/healthz/ready/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready", "pending_migrations": 0})
        executor.migration_plan.assert_called_once_with([("crank", "0018")])

    @patch("crank.views.health.MigrationExecutor")
    def test_not_ready_when_migrations_are_pending(self, executor_class):
        executor = executor_class.return_value
        executor.loader.graph.leaf_nodes.return_value = [("crank", "0018")]
        executor.migration_plan.return_value = [Mock(), Mock()]

        response = self.client.get("/healthz/ready/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(), {"status": "not_ready", "pending_migrations": 2}
        )

    @patch("crank.views.health.MigrationExecutor", side_effect=RuntimeError("DB down"))
    def test_not_ready_when_database_is_unreachable(self, executor_class):
        response = self.client.get("/healthz/ready/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(), {"status": "unavailable", "pending_migrations": None}
        )
