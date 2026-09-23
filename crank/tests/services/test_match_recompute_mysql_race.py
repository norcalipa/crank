# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Real MySQL two-connection races for versioned match recompute (issue #475).

Production runs MySQL, where ``select_for_update`` genuinely blocks a second
connection instead of the SQLite default suite's "database is locked"
failure. This module proves two of the invariants issue #475 exists to
guarantee, with real two-connection semantics (Django opens one connection
per thread):

1. **Snapshot/publish ordering.** Two ``recompute_user`` calls for the same
   user, barrier-synchronized so both open their snapshot before either
   publishes, must serialize on the ``MatchResultState`` row lock: exactly
   one generation ends up current, no duplicate ``JobMatch`` rows are
   created, and neither call raises.
2. **Dismiss vs. publish.** A dismiss racing a concurrent ranker-version-bump
   publish must not lose the dismissal: the view layer's dismiss path locks
   the same ``MatchResultState`` row :func:`crank.agents.jobs.match_persist.publish`
   locks (issue #475 review finding 3), so the two are serialized regardless
   of which wins the race.

The default suite runs SQLite, where a second writer connection would
deadlock at the file level rather than exercise ``select_for_update``
ordering, so this module skips there — patterned on
``crank/tests/test_mysql_concurrency.py`` and
``crank/tests/services/test_score_mysql_race.py``.

To run this module against a real MySQL server (InnoDB required, pymysql
from requirements.txt):

    # The DB user must be allowed to create and drop scratch
    # ``test_<DB_NAME>`` databases: use a disposable MySQL server, or grant
    # the standard test-database wildcard to the runner's user, e.g.
    #   GRANT ALL PRIVILEGES ON `test\\_%`.* TO '<user>'@'%';
    SECRET_KEY=test REDIS_MASTER_URL=redis://localhost:6379/0 \
    DB_NAME=crank_test DB_USER=<user> DB_PASS=<password> \
    DB_HOST=127.0.0.1 DB_PORT=3306 \
    python -m pytest crank/tests/services/test_match_recompute_mysql_race.py \
        --ds crank.settings.mysql_test --create-db -v

CI runs the default SQLite suite; this module is the operator-run MySQL
variant. The full pytest run stays green either way because the module
skips on non-MySQL backends.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from crank.agents.jobs.match_persist import ensure_match_result_state
from crank.agents.jobs.ranking_config import RankingConfig
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch, MatchResultState
from crank.models.organization import Organization
from crank.models.preference import UserPreference
from crank.services.match_recompute import RecomputeStatus, recompute_user


def make_listing(source, organization, *, title="Engineer"):
    now = timezone.now()
    return JobListing.all_objects.create(
        source=source,
        external_id=title.lower(),
        canonical_url=f"https://jobs.example.test/{title.lower()}",
        employer_name=organization.name,
        title=title,
        first_seen_at=now - timedelta(days=1),
        last_seen_at=now,
        status=JobListing.Status.ACTIVE,
        organization=organization,
    )


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "match-recompute-mysql-race-tests",
        }
    }
)
@skipUnless(connection.vendor == "mysql", "requires a MySQL (InnoDB) test database")
class MySqlRecomputeRaceTests(TransactionTestCase):
    """Two-connection races for the snapshot -> compute -> CAS-publish path.

    ``TransactionTestCase`` is required: ``TestCase`` wraps each test in a
    transaction, so a second connection could never observe the writes and
    ``select_for_update`` could never block a contender.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("owner", password="secret")
        self.organization = Organization.objects.create(name="Acme")
        self.source = JobSourceCatalog.objects.create(
            name="Synthetic",
            adapter_key="synthetic.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        self.listing = make_listing(self.source, self.organization)
        self.pref = UserPreference.objects.create(user=self.user, revision=0)

    def tearDown(self):
        # Writer threads get their own connections; close every one so the
        # test runner's teardown (flush) is not blocked by lingering sessions.
        connections.close_all()

    def test_two_connection_concurrent_recompute_serializes_and_yields_one_current(self):
        barrier = threading.Barrier(2, timeout=30)

        def _run(reason):
            try:
                barrier.wait(timeout=30)
                outcome = recompute_user(self.user, reason=reason)
                return ("ok", outcome)
            except Exception as exc:  # noqa: BLE001 - surfaced via the result
                return ("error", exc)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_run, "race-a"),
                executor.submit(_run, "race-b"),
            ]
            results = [future.result(timeout=60) for future in futures]

        self.assertEqual(
            [status for status, _ in results],
            ["ok", "ok"],
            f"concurrent recompute surfaced an error: {results}",
        )
        outcomes = sorted(outcome.status for _, outcome in results)
        # Whichever call's snapshot lock-acquisition is second sees the
        # first's published generation and is already CURRENT; there is no
        # way for one call to discard the other's result on a single,
        # unmodified user, so the pair is either two PUBLISHED (interleaved
        # snapshots, both genuinely necessary) or one PUBLISHED + one
        # CURRENT (the common case) -- never a DISCARDED_STALE or FAILED.
        self.assertTrue(
            set(outcomes).issubset(
                {RecomputeStatus.PUBLISHED, RecomputeStatus.CURRENT}
            ),
            f"unexpected outcome mix: {outcomes}",
        )
        self.assertEqual(JobMatch.objects.filter(user=self.user).count(), 1)
        state = MatchResultState.objects.get(user=self.user)
        self.assertIsNotNone(state.current_generation)
        self.assertEqual(state.issued_generation, state.current_generation)

    def test_two_connection_dismiss_races_ranker_version_bump_publish_without_loss(self):
        recompute_user(self.user, reason="preference")
        match = JobMatch.objects.get(user=self.user)
        self.assertFalse(match.dismissed)

        barrier = threading.Barrier(2, timeout=30)

        def _dismiss():
            try:
                barrier.wait(timeout=30)
                state = ensure_match_result_state(self.user)
                now = timezone.now()
                with transaction.atomic():
                    MatchResultState.objects.select_for_update().filter(
                        pk=state.pk
                    ).first()
                    JobMatch.objects.filter(
                        user=self.user,
                        listing_id=match.listing_id,
                        dismissed=False,
                    ).update(dismissed=True, modified=now)
                return ("ok", None)
            except Exception as exc:  # noqa: BLE001 - surfaced via the result
                return ("error", exc)
            finally:
                connections.close_all()

        def _publish_version_bump():
            try:
                barrier.wait(timeout=30)
                v2 = RankingConfig(version="2.0.0-race")
                outcome = recompute_user(self.user, reason="race", config=v2, force=True)
                return ("ok", outcome)
            except Exception as exc:  # noqa: BLE001 - surfaced via the result
                return ("error", exc)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_dismiss),
                executor.submit(_publish_version_bump),
            ]
            results = [future.result(timeout=60) for future in futures]

        self.assertEqual(
            [status for status, _ in results],
            ["ok", "ok"],
            f"dismiss/publish race surfaced an error: {results}",
        )
        # Every version-row for (user, listing) must end up dismissed,
        # regardless of which of the two writers committed first.
        rows = JobMatch.objects.filter(user=self.user, listing_id=match.listing_id)
        self.assertGreaterEqual(rows.count(), 1)
        self.assertTrue(
            all(row.dismissed for row in rows),
            f"a version row lost its dismissal: {[(r.ranker_version, r.dismissed) for r in rows]}",
        )
