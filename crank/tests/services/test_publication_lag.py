# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Publication-to-match lag measurement and aggregation (issue #482)."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from crank.models.job import JobSourceCatalog
from crank.models.organization import Organization
from crank.models.preference import UserPreference
from crank.models.publication import PublicationEvent
from crank.services import match_recompute
from crank.services.match_recompute import RecomputeStatus, publication_lag_seconds, recompute_user
from crank.tests.services.test_match_recompute import make_listing


def make_event(age_seconds):
    event = PublicationEvent.objects.create(
        target_type=list(PublicationEvent.TargetType)[0],
        target_id=1,
        event_kind=PublicationEvent.EventKind.CREATED,
    )
    PublicationEvent.objects.filter(pk=event.pk).update(
        created_at=timezone.now() - timedelta(seconds=age_seconds)
    )
    return event


class PublicationLagHelperTests(TestCase):
    def test_none_for_empty_or_inverted_window(self):
        self.assertIsNone(publication_lag_seconds(None, None))
        event = make_event(30)
        self.assertIsNone(publication_lag_seconds(event.id, event.id))
        self.assertIsNone(publication_lag_seconds(event.id + 5, event.id))

    def test_none_when_no_event_in_window(self):
        self.assertIsNone(publication_lag_seconds(None, 12345))

    def test_uses_oldest_event_in_window(self):
        old = make_event(100)
        newer = make_event(10)
        lag = publication_lag_seconds(None, newer.id)
        self.assertTrue(100 <= lag <= 103, lag)
        after_old = publication_lag_seconds(old.id, newer.id)
        self.assertTrue(10 <= after_old <= 13, after_old)

    def test_negative_clock_skew_clamps_to_zero(self):
        event = make_event(-60)
        self.assertEqual(publication_lag_seconds(None, event.id), 0)

    def test_explicit_now(self):
        event = make_event(0)
        later = timezone.now() + timedelta(seconds=500)
        self.assertGreaterEqual(publication_lag_seconds(None, event.id, now=later), 499)

    def test_query_failure_is_swallowed(self):
        with patch("crank.services.match_recompute.PublicationEvent.objects") as objects:
            objects.filter.side_effect = RuntimeError("db gone")
            self.assertIsNone(publication_lag_seconds(0, 5))


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class RecomputeLagTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("lag", password="secret")
        organization = Organization.objects.create(name="Acme")
        source = JobSourceCatalog.objects.create(
            name="Synthetic", adapter_key="synthetic.v1", base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED, enabled=True,
        )
        make_listing(source, organization)
        UserPreference.objects.create(user=self.user, revision=0)

    def test_first_generation_reports_no_lag(self):
        make_event(30 * 86400)
        first = recompute_user(self.user, reason="preference")
        self.assertEqual(first.status, RecomputeStatus.PUBLISHED)
        self.assertIsNone(first.publication_lag_seconds)

    def test_published_outcome_carries_lag_only_with_events(self):
        first = recompute_user(self.user, reason="preference")
        self.assertEqual(first.status, RecomputeStatus.PUBLISHED)
        self.assertIsNone(first.publication_lag_seconds)
        event = make_event(60)
        second = recompute_user(self.user, reason="drain")
        self.assertEqual(second.status, RecomputeStatus.PUBLISHED)
        self.assertTrue(60 <= second.publication_lag_seconds <= 65)
        self.assertIsNotNone(event)

    def test_current_outcome_has_no_lag(self):
        recompute_user(self.user, reason="preference")
        current = recompute_user(self.user, reason="drain")
        self.assertEqual(current.status, RecomputeStatus.CURRENT)
        self.assertIsNone(current.publication_lag_seconds)

    def test_drain_aggregates_lag(self):
        recompute_user(self.user, reason="preference")
        make_event(90)
        counts = match_recompute.drain(10)
        self.assertEqual(counts["publication_lag_count"], 1)
        self.assertTrue(90 <= counts["publication_lag_max_seconds"] <= 95)

    def test_drain_without_events_reports_zero(self):
        counts = match_recompute.drain(10)
        self.assertEqual(counts["publication_lag_count"], 0)
        self.assertEqual(counts["publication_lag_max_seconds"], 0)
