# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""AC1: preference-committed fast path (issue #475).

``TransactionTestCase`` because the #466 hook fires from
``transaction.on_commit``, which never runs inside the default
``TestCase`` savepoint-wrapped transaction.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings

from crank.models.job_match import MatchResultState
from crank.models.preference import UserPreference
from crank.services import preferences as prefs


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    MATCH_RECOMPUTE_ENABLED=True,
    PREFERENCE_RECOMPUTE_HOOK="crank.services.match_recompute.on_preference_committed",
)
class PreferenceCommittedFastPathTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", password="secret")

    def test_apply_patch_publishes_a_generation_from_db_inventory(self):
        with patch("crank.services.job_ingest.ingest_job_source") as ingest:
            prefs.apply_patch_to_user(self.user, {"set": {"notes": "remote only"}})
        ingest.assert_not_called()
        pref = UserPreference.objects.get(user=self.user)
        state = MatchResultState.objects.get(user=self.user)
        self.assertEqual(state.preference_revision, pref.revision)
        self.assertIsNotNone(state.current_generation)

    def test_undo_publishes_a_generation_with_the_restored_revision(self):
        with patch("crank.services.job_ingest.ingest_job_source"):
            first = prefs.apply_patch_to_user(
                self.user, {"set": {"notes": "remote only"}}
            )
        with patch("crank.services.job_ingest.ingest_job_source") as ingest:
            prefs.undo_preference_change(self.user, first["undo"])
        ingest.assert_not_called()
        pref = UserPreference.objects.get(user=self.user)
        state = MatchResultState.objects.get(user=self.user)
        self.assertEqual(state.preference_revision, pref.revision)

    def test_reset_publishes_a_generation_with_the_new_revision(self):
        with patch("crank.services.job_ingest.ingest_job_source"):
            prefs.apply_patch_to_user(self.user, {"set": {"notes": "remote only"}})
        with patch("crank.services.job_ingest.ingest_job_source") as ingest:
            prefs.reset(self.user)
        ingest.assert_not_called()
        pref = UserPreference.objects.get(user=self.user)
        state = MatchResultState.objects.get(user=self.user)
        self.assertEqual(state.preference_revision, pref.revision)

    def test_hook_exception_never_fails_the_save(self):
        with patch(
            "crank.services.match_recompute.on_preference_committed",
            side_effect=RuntimeError("boom"),
        ):
            result = prefs.apply_patch_to_user(
                self.user, {"set": {"notes": "remote only"}}
            )
        self.assertTrue(result["changed"])

    @override_settings(MATCH_RECOMPUTE_ENABLED=False)
    def test_hook_noops_when_flag_disabled(self):
        prefs.apply_patch_to_user(self.user, {"set": {"notes": "remote only"}})
        self.assertFalse(MatchResultState.objects.filter(user=self.user).exists())
