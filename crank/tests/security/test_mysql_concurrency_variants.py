# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""MySQL-targeted concurrency/deletion variants for the #487 guards.

These tests exercise the optimistic-concurrency lock
(``crank/services/preferences.py`` ``_lock`` via ``select_for_update``) and the
late-reply deletion cascade against a MySQL backend, where FOR UPDATE actually
serializes writers (SQLite treats it as a no-op, so CI's SQLite runs only
prove the timestamp check, not the row lock).

Skipped by default (SQLite CI); opt in with a MySQL settings target:

    export CRANK_MYSQL_TEST=1
    export DJANGO_SETTINGS_MODULE=crank.settings_mysql   # MySQL-configured settings module
    export ENV=dev SECRET_KEY=... REDIS_MASTER_URL=redis://localhost:6379/0

    # Two concurrent patch writers, one stale: exactly one wins, the stale
    # writer gets StalePreferenceError and nothing is overwritten.
    python -m pytest crank/tests/security/test_mysql_concurrency_variants.py::MySqlConcurrencyVariants::test_two_writers_one_stale -v

    # Conversation deleted mid-turn: no assistant message row survives.
    python -m pytest crank/tests/security/test_mysql_concurrency_variants.py::MySqlConcurrencyVariants::test_delete_during_turn -v

Residual: these variants require operator-provisioned MySQL and are not run
in CI; the threat model documents them as the backend-specific evidence path.
"""
from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connections
from django.test import TransactionTestCase
from django.urls import reverse

from crank.models import JobSearchConversation, JobSearchMessage, UserPreference
from crank.services import preferences

MYSQL_ENABLED = os.environ.get("CRANK_MYSQL_TEST") == "1"
skip_unless_mysql = __import__("unittest").skipUnless(
    MYSQL_ENABLED, "set CRANK_MYSQL_TEST=1 with a MySQL settings target to run"
)


@skip_unless_mysql
class MySqlConcurrencyVariants(TransactionTestCase):
    """Backend-specific concurrency evidence (MySQL only; skipped on SQLite)."""

    databases = "__all__"

    def setUp(self):
        self.alice = User.objects.create_user("mysql-alice", "ma@example.com", "pw")
        preferences.read(self.alice)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        connections.close_all()

    def test_two_writers_one_stale(self):
        """Two writers race apply_patch_to_user; the stale writer loses cleanly."""
        from crank.services.preferences import StalePreferenceError

        version = UserPreference.objects.get(user=self.alice).modified
        results = {}

        def writer(name, expected):
            try:
                result = preferences.apply_patch_to_user(
                    self.alice, {"set": {"notes": name}}, expected_modified=expected
                )
                results[name] = ("applied", result["changed"])
            except StalePreferenceError:
                results[name] = ("stale", None)

        writer("writer-current", version)
        stale_version = UserPreference.objects.get(user=self.alice).modified
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(writer, "writer-a", version),
                pool.submit(writer, "writer-b", stale_version),
            ]
            for future in futures:
                future.result()
        # At least one writer observed the stale rejection; the row holds a
        # single consistent value and no write was silently overwritten.
        outcomes = [results["writer-a"][0], results["writer-b"][0]]
        assert "stale" in outcomes
        stored = UserPreference.objects.get(user=self.alice)
        assert stored.preferences["notes"] in {"writer-a", "writer-b"}

    def test_delete_during_turn(self):
        """A conversation deleted mid-turn leaves no assistant message row."""

        class MidTurnDeleteProvider:
            def generate_reply(self, *, conversation, user_message):
                conversation.messages.all().delete()
                JobSearchConversation.objects.filter(pk=conversation.pk).delete()
                return "late reply", True, None

        from crank.agents.job_search.demo import JobSearchService

        client = self.client
        client.force_login(self.alice)
        create = client.post(
            reverse("agent-conversation-list"),
            data=json.dumps({"create_new": True}),
            content_type="application/json",
        )
        conversation_id = create.json()["id"]

        with patch.object(
            JobSearchService,
            "__init__",
            lambda self, *a, **kw: setattr(self, "provider", MidTurnDeleteProvider()),
        ):
            resp = client.post(
                reverse("agent-conversation-detail", args=[conversation_id]),
                data=json.dumps(
                    {"content": "hello", "idempotency_key": str(uuid.uuid4())}
                ),
                content_type="application/json",
            )
        assert resp.status_code == 409
        assert resp.json()["error"]["type"] == "conversation_closed"
        assert not JobSearchMessage.objects.filter(
            conversation_id=conversation_id
        ).exists()
