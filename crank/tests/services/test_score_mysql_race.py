# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Real MySQL two-connection first-write race for score tuples (issue #461).

Production runs MySQL, where Django cannot emit the partial unique
constraint on ``Score`` (active-only uniqueness, W036) and
``select_for_update()`` cannot lock an absent row: two transactions writing
the first score for the same empty ``(type, source, target)`` tuple could
both pass the "no active row" check and both insert. ``ScoreTupleAnchor``
closes that gap: racing first writers collide on the anchor's unconditional
unique constraint, then hold ``select_for_update`` on the anchor row for the
whole transaction, and the loser reconciles against the winner's committed
rows via locking reads (``crank/services/scores.py``).

This module proves the guarantee with real MySQL two-connection semantics:
two writer threads (Django opens one thread-local connection per thread)
pass a barrier and write the empty tuple through the production
``persist_score_observation`` path; exactly one active row must survive and
neither writer may surface an error (the API-layer "no 500" contract).

The default suite runs SQLite, where two writer transactions deadlock at
the file level ("database is locked"), so this module skips there; the
serialized two-connection variant that runs in the default suite lives in
``crank/tests/services/test_score_persistence.py``
(``ScoreFirstWriteRaceTests``).

To run this module against a real MySQL server (InnoDB required, pymysql
from requirements.txt):

    # The DB user must be allowed to create and drop scratch
    # ``test_<DB_NAME>`` databases: use a disposable MySQL server, or grant
    # the standard test-database wildcard to the runner's user, e.g.
    #   GRANT ALL PRIVILEGES ON `test\\_%`.* TO '<user>'@'%';
    SECRET_KEY=test REDIS_MASTER_URL=redis://localhost:6379/0 \
    DB_NAME=crank_test DB_USER=<user> DB_PASS=<password> \
    DB_HOST=127.0.0.1 DB_PORT=3306 \
    python -m pytest crank/tests/services/test_score_mysql_race.py \
        --ds crank.settings.mysql_test --create-db -v

CI runs the default SQLite suite; this module is the operator-run MySQL
variant. The full pytest run stays green either way because the module
skips on non-MySQL backends.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless

from django.core.cache import cache
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from crank.models.organization import Organization
from crank.models.score import Score, ScoreType
from crank.services import scores as score_services


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "score-mysql-race-tests",
        }
    }
)
@skipUnless(connection.vendor == "mysql", "requires a MySQL (InnoDB) test database")
class MySqlScoreFirstWriteRaceTests(TransactionTestCase):
    """Two-connection first-write race for one empty score tuple on MySQL.

    ``TransactionTestCase`` is required: ``TestCase`` wraps each test in a
    transaction, so a second connection could never observe the writes and
    the anchor-row lock could never block a contender.
    """

    def setUp(self):
        cache.clear()
        self.source = Organization.objects.create(
            name="Source Org", gives_ratings=True
        )
        self.target = Organization.objects.create(name="Target Org")
        self.score_type = ScoreType.objects.create(name="Race Culture")

    def tearDown(self):
        # Writer threads get their own connections; close every one so the
        # test runner's teardown (flush) is not blocked by lingering sessions.
        connections.close_all()

    @staticmethod
    def _race_provenance(value):
        return {
            "external_id": "race-461",
            "source_url": "https://ratings.example.com/org/race",
            "adapter_version": "v1",
            "observed_at": "2026-09-14T00:00:00Z",
            "raw_value": str(value),
        }

    def _persist_once(self, value):
        return score_services.persist_score_observation(
            source=self.source,
            target=self.target,
            score_type=self.score_type,
            value=value,
            provenance=self._race_provenance(value),
        ).outcome

    def _write_once(self, barrier, value):
        """One writer on its own dedicated, thread-local DB connection."""
        try:
            barrier.wait(timeout=30)
            outcome = self._persist_once(value)
            return ("ok", outcome)
        except Exception as exc:
            return ("error", exc)
        finally:
            connections.close_all()

    def _run_race(self, values):
        barrier = threading.Barrier(2, timeout=30)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self._write_once, barrier, value)
                for value in values
            ]
            return [future.result(timeout=60) for future in futures]

    def _tuple_scores(self):
        return Score.objects.filter(
            type=self.score_type, source=self.source, target=self.target
        )

    def test_two_connection_first_write_race_yields_exactly_one_active(self):
        # Barrier-synchronized first write on the empty tuple: both writers
        # must complete (no error surfaced to either, the API-layer "no
        # 500"), exactly one row is created, and the loser reconciles to a
        # noop against the winner's committed row.
        results = self._run_race([4.5, 4.5])
        self.assertEqual(
            [status for status, _ in results],
            ["ok", "ok"],
            f"MySQL first-write race surfaced an error: {results}",
        )
        outcomes = sorted(payload for _, payload in results)
        self.assertEqual(outcomes, ["created", "noop"])
        # No duplicate active row and no duplicate history row.
        self.assertEqual(self._tuple_scores().count(), 1)
        self.assertEqual(
            self._tuple_scores().filter(status=Score.ACTIVE_STATUS).count(), 1
        )

    def test_two_connection_race_distinct_values_supersedes_exactly_once(self):
        # Two genuinely concurrent writers with different values: the loser
        # takes over the anchor after the winner commits, supersedes the
        # winner's row exactly once, and leaves a single active row.
        results = self._run_race([3.0, 5.0])
        self.assertEqual(
            [status for status, _ in results],
            ["ok", "ok"],
            f"MySQL first-write race surfaced an error: {results}",
        )
        self.assertEqual(
            sorted(payload for _, payload in results), ["changed", "created"]
        )
        self.assertEqual(self._tuple_scores().count(), 2)
        self.assertEqual(
            self._tuple_scores().filter(status=Score.ACTIVE_STATUS).count(), 1
        )
