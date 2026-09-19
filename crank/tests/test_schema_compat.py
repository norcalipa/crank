# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Schema compatibility harness for epic #454 additive rollouts (issue #463).

These tests are the contract that every later epic schema PR keeps green:
existing schema-v2 preference documents, conversation idempotency keys, and
stored ``JobMatch`` rows must survive deployment and additive backfill
without change, and a bounded backfill must be resumable (a second run is a
no-op). No new schema is introduced here; this module proves the existing
shapes remain servable when future additive fields are absent, AND that
additive fields written by a newer schema version survive old-pod writes:
patch/reset preserve the unknown portion verbatim while rejecting patches
that target it (the mixed-version write contract is documented in
``docs/deployment-migrations.md``). Turn replay goes through the real
production write path (``crank.views.job_search.persist_idempotent_message``
``crank/views/job_search.py``); the MySQL two-connection race for that path
is proven separately by ``crank/tests/test_mysql_concurrency.py``.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import F
from django.test import TestCase
from django.utils import timezone

from crank.models.job import JobListing, JobSourceCatalog
from crank.models.job_match import JobMatch
from crank.models.job_search import JobSearchConversation, JobSearchMessage
from crank.models.organization import Organization
from crank.models.preference import UserPreference, default_preferences
from crank.services import preferences as preferences_service
from crank.services.preferences import (
    UnknownFieldError,
    to_markdown,
)
from crank.views.job_search import persist_idempotent_message

User = get_user_model()


class PreferenceSchemaCompatTests(TestCase):
    """Existing v1/v2 preference documents survive read/export/patch."""

    def setUp(self):
        self.user = User.objects.create_user("prefcompat", password="secret")

    def test_v2_document_round_trips_through_read_export_and_patch(self):
        """The canonical (current) default document loads, validates, and
        round-trips when future additive fields are absent."""
        UserPreference.objects.create(user=self.user, preferences=default_preferences())

        read_doc = preferences_service.read(user=self.user)
        self.assertEqual(read_doc["schema_version"], 3)
        self.assertEqual(read_doc["preferences"], default_preferences())

        exported = preferences_service.export(user=self.user)
        self.assertEqual(exported["schema_version"], 3)
        self.assertEqual(exported["preferences"], default_preferences())

        patch = {"set": {"notes": "prefers public transit"}}
        patched = preferences_service.apply_patch_to_user(self.user, patch)
        self.assertTrue(patched["changed"])
        self.assertEqual(patched["preferences"]["notes"], "prefers public transit")

        reread = preferences_service.read(user=self.user)
        self.assertEqual(reread["preferences"]["notes"], "prefers public transit")
        # Untouched sections are unchanged.
        self.assertEqual(reread["preferences"]["compensation"], default_preferences()["compensation"])

    def test_pre_v2_document_without_v2_fields_is_still_served(self):
        """A stored document missing the 0024-added keys (require_public_company,
        max_in_office_days) still reads and exports unchanged: the services do
        not require the additive v2 keys to serve old documents."""
        v2_doc = default_preferences()
        del v2_doc["compensation"]["require_public_company"]
        del v2_doc["work_location"]["max_in_office_days"]
        UserPreference.objects.create(
            user=self.user, preferences=v2_doc, schema_version=1
        )

        read_doc = preferences_service.read(user=self.user)
        self.assertNotIn("require_public_company", read_doc["preferences"]["compensation"])
        self.assertNotIn("max_in_office_days", read_doc["preferences"]["work_location"])

        exported = preferences_service.export(user=self.user)
        self.assertEqual(exported["schema_version"], 1)
        self.assertEqual(exported["preferences"], v2_doc)

    def test_v3_document_round_trips_through_read_export_patch_and_reset(self):
        """The v3 document (roles, importance, scope, and the new
        compensation/work_location keys) validates and round-trips through
        every service entry point, not just read/export."""
        UserPreference.objects.create(user=self.user, preferences=default_preferences())

        read_doc = preferences_service.read(user=self.user)
        self.assertEqual(read_doc["schema_version"], 3)

        exported = preferences_service.export(user=self.user)
        self.assertEqual(exported["schema_version"], 3)

        patch = {"set": {"roles.families": ["engineering"], "compensation.basis": "total"}}
        patched = preferences_service.apply_patch_to_user(self.user, patch)
        self.assertTrue(patched["changed"])
        self.assertEqual(patched["preferences"]["roles"]["families"], ["engineering"])
        self.assertEqual(patched["preferences"]["compensation"]["basis"], "total")

        reset_result = preferences_service.reset(user=self.user)
        self.assertTrue(reset_result["changed"])
        self.assertEqual(reset_result["preferences"], default_preferences())

    def test_v2_document_missing_v3_keys_is_still_served(self):
        """A stored document missing the 0035-added keys (roles, importance,
        scope, and the new compensation/work_location subkeys) still reads
        and exports unchanged: the services do not require the additive v3
        keys to serve old documents."""
        v3_doc = default_preferences()
        del v3_doc["roles"]
        del v3_doc["importance"]
        del v3_doc["scope"]
        for key in (
            "basis", "period", "minimum_total_compensation",
            "equity_liquidity_required", "acceptable_liquidity_events",
        ):
            del v3_doc["compensation"][key]
        del v3_doc["work_location"]["office_days_exact"]
        UserPreference.objects.create(user=self.user, preferences=v3_doc, schema_version=2)

        read_doc = preferences_service.read(user=self.user)
        self.assertNotIn("roles", read_doc["preferences"])
        self.assertNotIn("importance", read_doc["preferences"])
        self.assertNotIn("scope", read_doc["preferences"])
        self.assertNotIn("basis", read_doc["preferences"]["compensation"])
        self.assertNotIn("office_days_exact", read_doc["preferences"]["work_location"])

        exported = preferences_service.export(user=self.user)
        self.assertEqual(exported["schema_version"], 2)
        self.assertEqual(exported["preferences"], v3_doc)

    def test_document_with_future_additive_field_still_reads_and_exports(self):
        """A document carrying an extra (future, additive) section is served
        unchanged by the read/export path: additive fields must not break
        readers during a rolling deploy."""
        doc = default_preferences()
        doc["notifications"] = {"channel": "email", "quiet_hours": "22:00-07:00"}
        UserPreference.objects.create(user=self.user, preferences=doc)

        read_doc = preferences_service.read(user=self.user)
        self.assertEqual(
            read_doc["preferences"]["notifications"],
            {"channel": "email", "quiet_hours": "22:00-07:00"},
        )
        exported = preferences_service.export(user=self.user)
        self.assertEqual(exported["preferences"], doc)

    def test_mixed_version_patch_write_succeeds_and_preserves_additive_fields(self):
        """Old-shape write on a new-shape document: during a rolling deploy a
        new pod writes additive fields, then an old pod must still be able
        to patch the fields it knows without rejecting the document and
        without dropping or corrupting the additive fields. This is the
        write-path half of the mixed-version contract (the read/export half
        is covered above)."""
        doc = default_preferences()
        doc["notifications"] = {"channel": "email", "quiet_hours": "22:00-07:00"}
        doc["compensation"]["future_salary_band"] = "staff"
        UserPreference.objects.create(user=self.user, preferences=doc)

        patched = preferences_service.apply_patch_to_user(
            self.user, {"set": {"notes": "prefers public transit"}}
        )
        self.assertTrue(patched["changed"])
        self.assertEqual(patched["preferences"]["notes"], "prefers public transit")
        # Additive fields survive the old-pod write verbatim (top level…)
        self.assertEqual(
            patched["preferences"]["notifications"],
            {"channel": "email", "quiet_hours": "22:00-07:00"},
        )
        # …and nested inside a known section.
        self.assertEqual(patched["preferences"]["compensation"]["future_salary_band"], "staff")

        reread = preferences_service.read(user=self.user)
        self.assertEqual(reread["preferences"]["notifications"],
                         {"channel": "email", "quiet_hours": "22:00-07:00"})
        self.assertEqual(reread["preferences"]["compensation"]["future_salary_band"], "staff")
        self.assertEqual(reread["preferences"]["notes"], "prefers public transit")

    def test_patch_cannot_target_additive_fields(self):
        """Strict-patch rule (pinned): an old pod may write only the fields it
        knows. Patches that target unknown paths — set, whole-section set, or
        remove — are rejected with UnknownFieldError, so an old pod can
        never create or corrupt a newer schema version's fields."""
        doc = default_preferences()
        doc["notifications"] = {"channel": "email"}
        UserPreference.objects.create(user=self.user, preferences=doc)

        with self.assertRaises(UnknownFieldError):
            preferences_service.apply_patch_to_user(
                self.user, {"set": {"notifications.channel": "sms"}}
            )
        with self.assertRaises(UnknownFieldError):
            preferences_service.apply_patch_to_user(
                self.user, {"set": {"notifications": {"channel": "sms"}}}
            )
        with self.assertRaises(UnknownFieldError):
            preferences_service.apply_patch_to_user(
                self.user, {"remove": {"notifications": None}}
            )
        # Nothing was modified by the rejected patches.
        reread = preferences_service.read(user=self.user)
        self.assertEqual(reread["preferences"]["notifications"], {"channel": "email"})
        self.assertEqual(reread["preferences"], doc)

    def test_markdown_projection_excludes_additive_fields(self):
        """Markdown is a projection of the known fields only: an additive
        section never renders (and never leaks into an LLM prompt), but its
        presence does not break rendering of the rest of the document."""
        doc = default_preferences()
        doc["notifications"] = {"channel": "email", "quiet_hours": "22:00-07:00"}
        rendered = to_markdown(doc)
        self.assertNotIn("notifications", rendered)
        self.assertNotIn("quiet_hours", rendered)
        self.assertIn("# Career Preferences", rendered)

    def test_reset_preserves_additive_fields(self):
        """Reset returns the known fields to defaults but preserves additive
        fields owned by a newer schema version verbatim: resetting what this
        pod knows must never delete data it cannot interpret."""
        doc = default_preferences()
        doc["compensation"]["minimum_salary"] = 250000
        doc["notifications"] = {"channel": "email"}
        doc["compensation"]["future_salary_band"] = "staff"
        UserPreference.objects.create(user=self.user, preferences=doc)

        result = preferences_service.reset(user=self.user)
        self.assertTrue(result["changed"])
        # Known fields are back to defaults.
        self.assertIsNone(result["preferences"]["compensation"]["minimum_salary"])
        # Additive fields survive.
        self.assertEqual(result["preferences"]["notifications"], {"channel": "email"})
        self.assertEqual(result["preferences"]["compensation"]["future_salary_band"], "staff")
        reread = preferences_service.read(user=self.user)
        self.assertEqual(reread["preferences"]["notifications"], {"channel": "email"})


class ConversationIdempotencyCompatTests(TestCase):
    """Conversation replay via idempotency_key stays duplicate-free.

    These tests submit through the REAL production write path
    (``crank.views.job_search.persist_idempotent_message``) rather than a
    mirror, so the harness cannot drift from the view. The concurrent
    (two-connection) guarantee on production MySQL is proven separately by
    ``crank/tests/test_mysql_concurrency.py``.
    """

    def setUp(self):
        self.user = User.objects.create_user("chatcompat", password="secret")
        self.conversation = JobSearchConversation.objects.create(owner=self.user)

    def _submit_user_message(self, key, content="hello"):
        """Submit one user turn through the production write path."""
        message, _ = persist_idempotent_message(
            self.conversation,
            key,
            JobSearchMessage.Role.USER,
            {"content": content},
        )
        return message

    def test_retried_submission_does_not_duplicate_messages(self):
        """A retried submission with the same idempotency key replays the
        stored row instead of creating a duplicate."""
        first = self._submit_user_message("retry-key-1")
        replay = self._submit_user_message("retry-key-1")
        self.assertEqual(first.pk, replay.pk)
        self.assertEqual(
            JobSearchMessage.objects.filter(conversation=self.conversation).count(), 1
        )

    def test_distinct_keys_create_distinct_messages(self):
        """Different keys are separate turns (replay only collapses retries)."""
        self._submit_user_message("key-a")
        self._submit_user_message("key-b")
        self.assertEqual(
            JobSearchMessage.objects.filter(
                conversation=self.conversation, role=JobSearchMessage.Role.USER
            ).count(),
            2,
        )

    def test_unique_constraint_blocks_duplicate_idempotency_key(self):
        """The DB-level unique_jobsearch_message_idempotency constraint
        prevents concurrent duplicates of the same (conversation, key, role)."""
        JobSearchMessage.objects.create(
            conversation=self.conversation,
            idempotency_key="dup-key",
            role=JobSearchMessage.Role.ASSISTANT,
            content="first answer",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JobSearchMessage.objects.create(
                    conversation=self.conversation,
                    idempotency_key="dup-key",
                    role=JobSearchMessage.Role.ASSISTANT,
                    content="duplicate answer",
                )

    def test_empty_idempotency_key_is_not_constrained(self):
        """Rows with an empty idempotency_key are outside the partial unique
        constraint, so legacy/anonymous turns keep working."""
        JobSearchMessage.objects.create(
            conversation=self.conversation, idempotency_key="", role=JobSearchMessage.Role.USER
        )
        JobSearchMessage.objects.create(
            conversation=self.conversation, idempotency_key="", role=JobSearchMessage.Role.USER
        )
        self.assertEqual(
            JobSearchMessage.objects.filter(
                conversation=self.conversation, idempotency_key=""
            ).count(),
            2,
        )


class JobMatchCompatTests(TestCase):
    """Stored JobMatch rows keyed by (user, listing, preference_version,
    ranker_version) remain readable and deduplicated."""

    def setUp(self):
        self.user = User.objects.create_user("matchcompat", password="secret")
        self.org = Organization.objects.create(name="CompatCo", funding_round="A")
        self.source = JobSourceCatalog.objects.create(
            name="CompatSource",
            adapter_key="compat.v1",
            base_url="https://jobs.example.test",
            enabled=True,
        )
        now = timezone.now()
        self.listing = JobListing.all_objects.create(
            source=self.source,
            external_id="compat-1",
            canonical_url="https://jobs.example.test/compat-1",
            employer_name="CompatCo",
            title="Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=self.org,
        )
        self.match = JobMatch.objects.create(
            user=self.user,
            listing=self.listing,
            organization=self.org,
            preference_version=3,
            ranker_version="compat.v1",
            score=0.75,
            first_matched_at=now - timedelta(hours=2),
            last_matched_at=now,
            seen_at=now - timedelta(hours=1),
            dismissed=False,
        )

    def test_stored_match_row_remains_readable_by_version_key(self):
        """Re-fetching by the (user, listing, preference_version,
        ranker_version) key returns the stored row with its seen/dismissed
        state intact."""
        found = JobMatch.objects.get(
            user=self.user,
            listing=self.listing,
            preference_version=3,
            ranker_version="compat.v1",
        )
        self.assertEqual(found.pk, self.match.pk)
        self.assertEqual(found.score, 0.75)
        self.assertIsNotNone(found.seen_at)
        self.assertFalse(found.dismissed)

    def test_match_version_key_still_deduplicates(self):
        """The unique_job_match_version constraint still blocks a duplicate
        row for the same version key."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JobMatch.objects.create(
                    user=self.user,
                    listing=self.listing,
                    preference_version=3,
                    ranker_version="compat.v1",
                    score=0.9,
                    first_matched_at=timezone.now(),
                    last_matched_at=timezone.now(),
                )

    def test_new_preference_version_is_a_distinct_row(self):
        """A different preference_version is a new row, not a collision."""
        JobMatch.objects.create(
            user=self.user,
            listing=self.listing,
            preference_version=4,
            ranker_version="compat.v1",
            score=0.8,
            first_matched_at=timezone.now(),
            last_matched_at=timezone.now(),
        )
        self.assertEqual(JobMatch.objects.filter(user=self.user).count(), 2)


class BoundedBackfillResumeTests(TestCase):
    """The bounded, resumable backfill pattern later schema PRs must follow.

    Runs the backfill twice: the second run is a no-op because already
    backfilled rows are never re-processed.
    """

    def _backfill_seen_at(self, batch_size=2):
        """Bounded, resumable backfill: stamp seen_at from first_matched_at
        for rows that do not have it yet, batch_size rows at a time."""
        updated = 0
        while True:
            ids = list(
                JobMatch.objects.filter(seen_at__isnull=True)
                .order_by("id")
                .values_list("id", flat=True)[:batch_size]
            )
            if not ids:
                return updated
            updated += JobMatch.objects.filter(id__in=ids, seen_at__isnull=True).update(
                seen_at=F("first_matched_at")
            )

    def _make_match(self, preference_version, seen_at=None):
        now = timezone.now()
        return JobMatch.objects.create(
            user=self.user,
            listing=self.listing,
            preference_version=preference_version,
            ranker_version="backfill.v1",
            score=0.5,
            first_matched_at=now - timedelta(days=1),
            last_matched_at=now,
            seen_at=seen_at,
        )

    def setUp(self):
        self.user = User.objects.create_user("backfillcompat", password="secret")
        self.org = Organization.objects.create(name="BackfillCo", funding_round="A")
        self.source = JobSourceCatalog.objects.create(
            name="BackfillSource",
            adapter_key="backfill.v1",
            base_url="https://jobs.example.test",
            enabled=True,
        )
        now = timezone.now()
        self.existing_seen_at = now
        self.listing = JobListing.all_objects.create(
            source=self.source,
            external_id="backfill-1",
            canonical_url="https://jobs.example.test/backfill-1",
            employer_name="BackfillCo",
            title="Engineer",
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now,
            status=JobListing.Status.ACTIVE,
            organization=self.org,
        )
        # Three rows missing seen_at (the "old rows" a backfill targets) and
        # one already-seen row that a resume must not touch. Each row uses a
        # distinct preference_version: the unique_job_match_version constraint
        # keys on (user, listing, preference_version, ranker_version).
        self.pending = [
            self._make_match(preference_version=version) for version in (1, 2, 3)
        ]
        self.existing_seen = self._make_match(
            preference_version=4, seen_at=self.existing_seen_at
        )

    def test_backfill_is_resumable_and_second_run_is_a_noop(self):
        """First run backfills exactly the pending rows; rerunning processes
        nothing and never overwrites a pre-existing seen_at."""
        self.assertEqual(self._backfill_seen_at(), 3)

        for match in self.pending:
            match.refresh_from_db()
            self.assertEqual(match.seen_at, match.first_matched_at)
        self.existing_seen.refresh_from_db()
        # The pre-existing seen_at is preserved untouched: the resume must
        # never overwrite a row it did not backfill.
        self.assertEqual(self.existing_seen.seen_at, self.existing_seen_at)
        self.assertNotEqual(
            self.existing_seen.seen_at, self.existing_seen.first_matched_at
        )

        # Resume: bounded backfill is idempotent; the second run is a no-op.
        self.assertEqual(self._backfill_seen_at(), 0)
        self.assertEqual(
            JobMatch.objects.filter(seen_at__isnull=True).count(), 0
        )
