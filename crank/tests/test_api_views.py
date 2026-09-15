# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings, RequestFactory
from django.urls import reverse
from django.core.cache import cache
from django.conf import settings
from crank.models.organization import Organization
from crank.models.company_profile import CompanyProfileObservation
from crank.models.score import Score, ScoreType
from crank.views import api
import json
from unittest.mock import patch
from datetime import datetime, timezone as dt_timezone

@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class ApiViewsTestCase(TestCase):
    def setUp(self):
        # Clear cache before each test
        cache.clear()

        # Create a test organization
        self.org = Organization.objects.create(
            name="Test Organization",
            status=1,  # Active status
            type="C",  # Company
            url="https://example.com",
            gives_ratings=True,
            public=True,
            accelerated_vesting=True,
            funding_round="S",  # Seed
            rto_policy="R",  # Remote
        )

        # Create a client for making requests
        self.client = Client()
        self.factory = RequestFactory()

    def tearDown(self):
        # Clear cache after each test
        cache.clear()

    def test_funding_round_choices(self):
        # Ensure the cache is empty
        cache.delete('funding_round_choices')

        # First request (should hit the database)
        response = self.client.get(reverse('funding_round_choices'))
        self.assertEqual(response.status_code, 200)

        # Parse the JSON response
        data = json.loads(response.content)

        # Verify the response contains expected choices
        self.assertIn('S', data)
        self.assertEqual(data['S'], 'Seed')

        # Should have all the funding round choices
        self.assertEqual(len(data), len(Organization.FundingRound.choices))

        # Second request (should hit the cache)
        response2 = self.client.get(reverse('funding_round_choices'))
        self.assertEqual(response2.status_code, 200)

        # Response should be the same
        self.assertEqual(response.content, response2.content)

    def test_funding_round_choices_cache_flow(self):
        """Test the caching logic flow in the funding_round_choices view"""
        # Make the first request - this should populate the cache
        response = self.client.get(reverse('funding_round_choices'))
        self.assertEqual(response.status_code, 200)

        # Make a second request - verify the response is consistent
        response2 = self.client.get(reverse('funding_round_choices'))
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(response.content, response2.content)

        # Verify the basic content - contains Seed round
        data = json.loads(response.content)
        self.assertIn('S', data)
        self.assertEqual(data['S'], 'Seed')

    def test_rto_policy_choices(self):
        # Ensure the cache is empty
        cache.delete('rto_policy_choices')

        # First request (should hit the database)
        response = self.client.get(reverse('rto_policy_choices'))
        self.assertEqual(response.status_code, 200)

        # Parse the JSON response
        data = json.loads(response.content)

        # Verify the response contains expected choices
        self.assertIn('R', data)
        self.assertEqual(data['R'], 'Remote')

        # Should have all the RTO policy choices
        self.assertEqual(len(data), len(Organization.RTOPolicy.choices))

        # Second request (should hit the cache)
        response2 = self.client.get(reverse('rto_policy_choices'))
        self.assertEqual(response2.status_code, 200)

        # Response should be the same
        self.assertEqual(response.content, response2.content)

    def test_rto_policy_choices_cache_flow(self):
        """Test the caching logic flow in the rto_policy_choices view"""
        # Make the first request - this should populate the cache
        response = self.client.get(reverse('rto_policy_choices'))
        self.assertEqual(response.status_code, 200)

        # Make a second request - verify the response is consistent
        response2 = self.client.get(reverse('rto_policy_choices'))
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(response.content, response2.content)

        # Verify the basic content - contains Remote option
        data = json.loads(response.content)
        self.assertIn('R', data)
        self.assertEqual(data['R'], 'Remote')

    def test_organization_detail(self):
        # First request (should hit the database)
        response = self.client.get(reverse('organization-detail', args=[self.org.id]))
        self.assertEqual(response.status_code, 200)

        # Parse the JSON response
        data = json.loads(response.content)

        # Verify the response contains expected data
        self.assertEqual(data['id'], self.org.id)
        self.assertEqual(data['name'], "Test Organization")
        self.assertEqual(data['type'], "C")
        self.assertEqual(data['url'], "https://example.com")
        self.assertEqual(data['gives_ratings'], True)
        self.assertEqual(data['public'], True)
        self.assertEqual(data['accelerated_vesting'], True)
        self.assertEqual(data['funding_round'], "S")
        self.assertEqual(data['rto_policy'], "R")

        # Second request (should hit the cache)
        response = self.client.get(reverse('organization-detail', args=[self.org.id]))
        self.assertEqual(response.status_code, 200)

    def test_organization_detail_not_found(self):
        # Request for a non-existent organization
        response = self.client.get(reverse('organization-detail', args=[99999]))
        self.assertEqual(response.status_code, 404)

    def test_organization_detail_inactive(self):
        # Create an inactive organization
        inactive_org = Organization.objects.create(
            name="Inactive Organization",
            status=0,  # Inactive status
            type="C",
            url="https://inactive.com",
            funding_round="S",
            rto_policy="R",
        )

        # Request for an inactive organization should return 404
        response = self.client.get(reverse('organization-detail', args=[inactive_org.id]))
        self.assertEqual(response.status_code, 404)

    def test_organization_scores(self):
        # First we need to create some scores for the organization
        # This would typically be done through a related model, but for testing purposes
        # we can mock the avg_scores method

        original_avg_scores = Organization.avg_scores

        # Mock the avg_scores method
        def mock_avg_scores(self):
            return [
                {'type__name': 'Culture', 'avg_score': 4.5},
                {'type__name': 'Leadership', 'avg_score': 3.8}
            ]

        # Apply the mock
        Organization.avg_scores = mock_avg_scores

        try:
            # First request (should hit the database)
            response = self.client.get(reverse('organization-scores', args=[self.org.id]))
            self.assertEqual(response.status_code, 200)

            # Parse the JSON response
            data = json.loads(response.content)

            # Verify the response contains expected data
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]['type__name'], 'Culture')
            self.assertEqual(data[0]['avg_score'], 4.5)
            self.assertEqual(data[1]['type__name'], 'Leadership')
            self.assertEqual(data[1]['avg_score'], 3.8)

            # Second request (should hit the cache)
            response = self.client.get(reverse('organization-scores', args=[self.org.id]))
            self.assertEqual(response.status_code, 200)
        finally:
            # Restore the original method
            Organization.avg_scores = original_avg_scores

    def test_organization_scores_not_found(self):
        # Request for a non-existent organization
        response = self.client.get(reverse('organization-scores', args=[99999]))
        self.assertEqual(response.status_code, 404)

    def test_organization_scores_inactive(self):
        # Create an inactive organization
        inactive_org = Organization.objects.create(
            name="Inactive Organization",
            status=0,  # Inactive status
            type="C",
            url="https://inactive.com",
            funding_round="S",
            rto_policy="R",
        )

        # Request for an inactive organization should return 404
        response = self.client.get(reverse('organization-scores', args=[inactive_org.id]))
        self.assertEqual(response.status_code, 404)

    def test_organization_scores_ignores_pre_fix_cache_entry(self):
        """Post-deploy rollout regression (issue #461 review, finding 1).

        A deploy swaps the code while the shared cache keeps whatever the
        previous build wrote. Seed both unversioned keys (view-level and
        model-level) with what the pre-#461 build cached -- a payload that
        averages the superseded row -- and prove the endpoint recomputes
        active-only rows from the versioned keys instead.
        """
        score_type = ScoreType.objects.create(name='Rollout Culture')
        Score.objects.create(
            source=self.org, target=self.org, type=score_type,
            score=1.0, status=Score.INACTIVE_STATUS,
        )
        Score.objects.create(
            source=self.org, target=self.org, type=score_type, score=5.0
        )
        pre_fix_rows = [{'type__name': 'Rollout Culture', 'avg_score': 3.0}]
        cache.set(f'organization_scores_api_{self.org.id}', pre_fix_rows)
        cache.set(f'organization_{self.org.id}_avg_scores', pre_fix_rows)

        response = self.client.get(reverse('organization-scores', args=[self.org.id]))

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        # Only the active replacement row is served; the pre-fix entries that
        # averaged the superseded 1.0 row are never reachable.
        self.assertEqual(data, [{'type__name': 'Rollout Culture', 'avg_score': 5.0}])
        # The recomputed payload is cached under the versioned key, and the
        # pre-fix entries remain untouched under their legacy keys (they age
        # out via TTL; no post-deploy reader ever consults them).
        self.assertEqual(
            cache.get(api.organization_scores_api_cache_key(self.org.id)),
            [{'type__name': 'Rollout Culture', 'avg_score': 5.0}],
        )
        self.assertEqual(
            cache.get(f'organization_scores_api_{self.org.id}'), pre_fix_rows
        )
        self.assertEqual(
            cache.get(f'organization_{self.org.id}_avg_scores'), pre_fix_rows
        )

    # --- Provenance endpoint tests ---

    def test_organization_provenance_no_observation(self):
        """Provenance for an org with no crawl observations."""
        response = self.client.get(reverse('organization-provenance', args=[self.org.id]))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['organization_id'], self.org.id)
        self.assertIsNotNone(data['organization_modified'])
        self.assertIsNotNone(data['organization_created'])
        self.assertIsNone(data['latest_observation'])

    def test_organization_provenance_with_observation(self):
        """Provenance surfaces the latest CompanyProfileObservation."""
        obs = CompanyProfileObservation.objects.create(
            organization=self.org,
            source_url='https://example.com/about',
            observed_domain='example.com',
            observed_name='Test Org',
            observed_at=datetime(2025, 1, 15, tzinfo=dt_timezone.utc),
            extraction_version='v1.2.3',
            status=CompanyProfileObservation.Status.AUTO_APPLIED,
        )
        response = self.client.get(reverse('organization-provenance', args=[self.org.id]))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['latest_observation']['source_url'], 'https://example.com/about')
        self.assertEqual(data['latest_observation']['observed_domain'], 'example.com')
        self.assertEqual(data['latest_observation']['extraction_version'], 'v1.2.3')
        self.assertEqual(data['latest_observation']['status'], 'auto_applied')

    def test_organization_provenance_latest_observation_wins(self):
        """Only the most recent observation is returned."""
        CompanyProfileObservation.objects.create(
            organization=self.org,
            source_url='https://old.com',
            observed_domain='old.com',
            observed_at=datetime(2024, 1, 1, tzinfo=dt_timezone.utc),
            extraction_version='v0.1',
            status=CompanyProfileObservation.Status.PENDING,
        )
        CompanyProfileObservation.objects.create(
            organization=self.org,
            source_url='https://new.com',
            observed_domain='new.com',
            observed_at=datetime(2025, 6, 1, tzinfo=dt_timezone.utc),
            extraction_version='v2.0',
            status=CompanyProfileObservation.Status.ACCEPTED,
        )
        response = self.client.get(reverse('organization-provenance', args=[self.org.id]))
        data = json.loads(response.content)
        self.assertEqual(data['latest_observation']['observed_domain'], 'new.com')
        self.assertEqual(data['latest_observation']['extraction_version'], 'v2.0')

    def test_organization_provenance_not_found(self):
        response = self.client.get(reverse('organization-provenance', args=[99999]))
        self.assertEqual(response.status_code, 404)

    def test_organization_provenance_inactive(self):
        inactive_org = Organization.objects.create(
            name="Inactive Org",
            status=0,
            type="C",
            url="https://inactive.com",
            funding_round="S",
            rto_policy="R",
        )
        response = self.client.get(reverse('organization-provenance', args=[inactive_org.id]))
        self.assertEqual(response.status_code, 404)

    def test_organization_provenance_cached(self):
        """Second request should return the same data (cached)."""
        response1 = self.client.get(reverse('organization-provenance', args=[self.org.id]))
        self.assertEqual(response1.status_code, 200)
        response2 = self.client.get(reverse('organization-provenance', args=[self.org.id]))
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(response1.content, response2.content)

    # --- Public cache purity (issue #470) ---

    def test_public_org_api_cache_is_identical_across_accounts_and_user_free(self):
        """Cached organization payloads never contain another account's data."""
        user_a = User.objects.create_user(
            "cache-user-a", "cache-user-a@example.com", "pw12345"
        )
        user_b = User.objects.create_user(
            "cache-user-b", "cache-user-b@example.com", "pw12345"
        )
        self.client.force_login(user_a)
        first = self.client.get(reverse('organization-detail', args=[self.org.id]))
        self.assertEqual(first.status_code, 200)
        self.client.force_login(user_b)
        second = self.client.get(reverse('organization-detail', args=[self.org.id]))
        self.assertEqual(second.status_code, 200)
        # The second account is served the cached payload: identical, and free
        # of either account's identifiers.
        self.assertEqual(first.content, second.content)
        for marker in (
            b"cache-user-a",
            b"cache-user-b",
            b"@example.com",
        ):
            self.assertNotIn(marker, second.content)

    def test_public_provenance_cache_is_identical_across_accounts_and_user_free(self):
        user_a = User.objects.create_user(
            "cache-user-a", "cache-user-a@example.com", "pw12345"
        )
        user_b = User.objects.create_user(
            "cache-user-b", "cache-user-b@example.com", "pw12345"
        )
        self.client.force_login(user_a)
        first = self.client.get(reverse('organization-provenance', args=[self.org.id]))
        self.assertEqual(first.status_code, 200)
        self.client.force_login(user_b)
        second = self.client.get(reverse('organization-provenance', args=[self.org.id]))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.content, second.content)
        self.assertNotIn(b"cache-user-a", second.content)
        self.assertNotIn(b"cache-user-b", second.content)

    # --- Account identity endpoint (issue #470 nav hydration) ---

    def test_account_whoami_anonymous(self):
        response = self.client.get(reverse('account-whoami'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"authenticated": False})
        # Non-cacheable so account switching can never replay a stale
        # identity (issue #470 review).
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_account_whoami_returns_only_callers_own_username(self):
        user = User.objects.create_user(
            "whoami-user", "whoami-user@example.com", "pw12345"
        )
        self.client.force_login(user)
        response = self.client.get(reverse('account-whoami'))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(
            data, {"authenticated": True, "username": "whoami-user"}
        )
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_account_whoami_sets_csrf_cookie_for_cached_shell_logout(self):
        """@ensure_csrf_cookie: the token-free cached shell's JS logout can
        pass the CSRF check with the cookie this response guarantees."""
        response = self.client.get(reverse('account-whoami'))
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)
