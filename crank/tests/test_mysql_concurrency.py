# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""MySQL-backed concurrent-turn-replay validation (issue #463, epic #454).

Production runs MySQL, where Django cannot create the conditional
``unique_jobsearch_message_idempotency`` partial unique index (W036 warnings
are expected; see ``docs/deployment-migrations.md``). The first-write-wins
guarantee for conversation turns is therefore provided by serializing
writers on the parent conversation row
(``crank.views.job_search.persist_idempotent_message``), following the
``crank/services/scores.py`` ``_persist_locked`` precedent.

This module proves that guarantee with real MySQL two-connection
semantics: two threads (Django opens one connection per thread) race a
submission with the same (conversation, idempotency_key, role) and exactly
one row must survive, with the loser replaying the winner's row. Without
the conversation-row lock, both connections miss the get-or-create lookup
and both inserts succeed on MySQL (there is no partial index to reject
one), so a green run here proves the serialization, not the constraint.

Skip-by-default: the repo's default test settings use SQLite
(``crank/settings/dev.py``), where ``select_for_update`` is a no-op and the
race is instead guarded by the partial unique constraint exercised in
``crank/tests/test_schema_compat.py``. This module is skipped there.

To run this module against a real MySQL server (InnoDB required):

    # 1. Start a Django-supported MySQL (8.4+, InnoDB) and make sure the
    #    runner can create the ``test_<DB_NAME>`` database (CREATE/DROP
    #    rights). A disposable local instance suffices — it never touches
    #    the production database.
    #
    # 2. Point Django at it via the test-only MySQL target
    #    ``crank/settings_mysql.py`` (credentials come from the
    #    ``CRANK_MYSQL_*`` environment, never from committed files):
    #      export ENV=dev SECRET_KEY=dev-test-secret \
    #             REDIS_MASTER_URL=redis://localhost:6379/0
    #      export DJANGO_SETTINGS_MODULE=crank.settings_mysql
    #      export CRANK_MYSQL_NAME=crank_test CRANK_MYSQL_USER=root \
    #             CRANK_MYSQL_PASSWORD=... CRANK_MYSQL_HOST=127.0.0.1 \
    #             CRANK_MYSQL_PORT=3306
    #
    # 3. Run only this module (the env var takes precedence over the
    #    pytest.ini default of crank.settings):
    #      python -m pytest crank/tests/test_mysql_concurrency.py -v
    #
    #    Alternatively, the staging settings read DB_NAME/DB_HOST/DB_PORT/
    #    DB_USER/DB_PASS (see ``crank/settings/staging.py``); they require
    #    the dj-db-conn-pip pool backend (requirements.txt).
    #
    # CI (run-tests.yml) intentionally runs the default SQLite suite; this
    # module is the operator-run MySQL variant. The full pytest run stays
    # green either way because the module skips on non-MySQL backends.
"""
import threading
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase

from crank.models.job_search import JobSearchConversation, JobSearchMessage
from crank.views.job_search import persist_idempotent_message

User = get_user_model()

# Rounds of the two-writer race per test. One round proves the mechanism;
# several rounds make an intermittent race loss reliably visible.
RACE_ROUNDS = 8


@skipUnless(connection.vendor == "mysql", "requires a MySQL (InnoDB) test database")
class MySqlConcurrentTurnReplayTests(TransactionTestCase):
    """Two-connection first-write race for turn idempotency on MySQL.

    ``TransactionTestCase`` is required: ``TestCase`` wraps each test in a
    transaction, so a second connection could never observe the writes and
    the row lock could never block a contender.
    """

    def setUp(self):
        self.user = User.objects.create_user("mysqlrace", password="secret")
        self.conversation = JobSearchConversation.objects.create(owner=self.user)

    def tearDown(self):
        # Threads get their own connections; close every one of them so the
        # test runner's teardown (flush) is not blocked by lingering sessions.
        connections.close_all()

    def _race_submission(self, key):
        """Two threads submit the same idempotency key concurrently.

        Returns the per-thread outcomes (message pk, created) or the raised
        exception, plus the row count for the key once both threads finish.
        """
        barrier = threading.Barrier(2)
        outcomes = [None, None]

        def submit(index):
            try:
                barrier.wait(timeout=10.0)
                message, created = persist_idempotent_message(
                    self.conversation,
                    key,
                    JobSearchMessage.Role.USER,
                    {"content": f"turn from writer {index}"},
                )
                outcomes[index] = (message.pk, created)
            except Exception as exc:  # surfaced to the main thread below
                outcomes[index] = exc

        threads = [
            threading.Thread(target=submit, args=(index,), name=f"mysql-race-{index}")
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        count = JobSearchMessage.objects.filter(
            conversation=self.conversation,
            idempotency_key=key,
            role=JobSearchMessage.Role.USER,
        ).count()
        return outcomes, count

    def test_concurrent_first_write_yields_exactly_one_row(self):
        """Each race round leaves exactly one row for the key and both
        writers observe the same message (one creator, one replayer)."""
        for round_no in range(RACE_ROUNDS):
            key = f"mysql-race-{round_no}"
            with self.subTest(round=round_no, key=key):
                outcomes, count = self._race_submission(key)

                # No writer crashed (a lost exception would otherwise look
                # like a clean single-writer win).
                for outcome in outcomes:
                    self.assertNotIsInstance(
                        outcome, Exception, f"writer failed: {outcome!r}"
                    )
                    self.assertIsNotNone(outcome)

                # Exactly one row for the raced key.
                self.assertEqual(count, 1)

                # Both writers replayed the same persisted message.
                pks = {outcome[0] for outcome in outcomes}
                created_flags = [outcome[1] for outcome in outcomes]
                self.assertEqual(len(pks), 1)
                self.assertEqual(sum(created_flags), 1)

    def test_serialized_replay_keeps_message_count_stable(self):
        """After N raced rounds the conversation holds exactly N user
        messages: races never accumulate duplicates over time."""
        for round_no in range(RACE_ROUNDS):
            self._race_submission(f"mysql-serial-{round_no}")
        self.assertEqual(
            JobSearchMessage.objects.filter(
                conversation=self.conversation,
                role=JobSearchMessage.Role.USER,
            ).count(),
            RACE_ROUNDS,
        )

    def test_empty_idempotency_key_stays_unconstrained_on_mysql(self):
        """Legacy turns without an idempotency key are outside the guarantee
        (no key, nothing to deduplicate): two rows may exist, on MySQL too."""
        for index in range(2):
            persist_idempotent_message(
                self.conversation,
                "",
                JobSearchMessage.Role.USER,
                {"content": f"legacy turn {index}"},
            )
        self.assertEqual(
            JobSearchMessage.objects.filter(
                conversation=self.conversation,
                idempotency_key="",
                role=JobSearchMessage.Role.USER,
            ).count(),
            2,
        )
