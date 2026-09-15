# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""MySQL-targeted concurrency/deletion variants for the #487 guards.

These tests exercise the optimistic-concurrency lock
(``crank/services/preferences.py`` ``_lock`` via ``select_for_update``) and the
late-reply deletion cascade against a MySQL backend, where FOR UPDATE actually
serializes writers (SQLite treats it as a no-op, so CI's SQLite runs only
prove the timestamp check, not the row lock).

Skipped by default (SQLite CI); opt in with the MySQL settings target shipped
as ``crank.settings_mysql`` (a thin settings module that inherits the default
settings and points ``DATABASES`` at an operator-provisioned MySQL server via
the ``CRANK_MYSQL_*`` environment variables — all credentials stay in the
environment, none are committed):

    export CRANK_MYSQL_TEST=1
    export DJANGO_SETTINGS_MODULE=crank.settings_mysql
    export ENV=dev SECRET_KEY=... REDIS_MASTER_URL=redis://localhost:6379/0
    export CRANK_MYSQL_NAME=crank_test CRANK_MYSQL_USER=... CRANK_MYSQL_PASSWORD=...

    # Two writers start from the SAME current version behind a barrier:
    # exactly one applies, the loser receives StalePreferenceError, and no
    # write is silently overwritten.
    python -m pytest crank/tests/security/test_mysql_concurrency_variants.py::MySqlConcurrencyVariants::test_two_same_version_writers_exactly_one_applies -v

    # Conversation deleted mid-turn: no assistant message row survives.
    python -m pytest crank/tests/security/test_mysql_concurrency_variants.py::MySqlConcurrencyVariants::test_delete_during_turn -v

Residual: these variants require operator-provisioned MySQL and are not run
in CI; the threat model documents them as the backend-specific evidence path.
"""
from __future__ import annotations

import json
import os
import threading
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

    def test_two_same_version_writers_exactly_one_applies(self):
        """Two writers start from the SAME current version behind a barrier.

        The outcome depends on the lock race itself (issue #487 review
        MINOR-3): ``apply_patch_to_user`` serializes both writers on the row
        lock (``select_for_update``), so the first commit bumps ``modified``
        and the second writer — holding the same, now-stale baseline — is
        rejected with ``StalePreferenceError``. Exactly one applies; no write
        is silently overwritten.
        """
        from crank.services.preferences import StalePreferenceError

        version = UserPreference.objects.get(user=self.alice).modified
        results = {}
        barrier = threading.Barrier(2)

        def writer(name):
            # Both writers begin from the same current version; the barrier
            # guarantees neither starts before both have captured it.
            barrier.wait()
            try:
                result = preferences.apply_patch_to_user(
                    self.alice, {"set": {"notes": name}}, expected_modified=version
                )
                results[name] = ("applied", result["changed"])
            except StalePreferenceError:
                results[name] = ("stale", None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(writer, "writer-a"),
                pool.submit(writer, "writer-b"),
            ]
            for future in futures:
                future.result()

        outcomes = sorted(outcome for outcome, _ in results.values())
        assert outcomes == ["applied", "stale"], results
        # The row holds exactly the winner's value at a single new version.
        stored = UserPreference.objects.get(user=self.alice)
        assert stored.preferences["notes"] in {"writer-a", "writer-b"}
        assert stored.modified != version

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
