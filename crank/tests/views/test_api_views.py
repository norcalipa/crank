# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import json
from datetime import timedelta

from django.test import TestCase, Client, RequestFactory, override_settings
from django.urls import reverse
from django.core.cache import cache
from django.conf import settings
from django.utils import timezone

from crank.models.company_profile import (
    CompanyFieldEvidence,
    CompanyProfileObservation,
)
from crank.models.organization import Organization
from crank.models.publication import PublicationEvent
from crank.models.score import Score, ScoreType
from crank.services.company_evidence import (
    FIELD_FRESHNESS_POLICY,
    accept_observation_fields,
)
from crank.services.publication import affected_keys
from crank.services.scores import organization_provenance_api_cache_key
from crank.views.fundinground import FundingRoundChoicesView
from crank.views.rtopolicy import RTOPolicyChoicesView


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class ApiViewsTest(TestCase):
    def setUp(self):
        # Clear cache before each test
        cache.clear()
        self.client = Client()
        self.factory = RequestFactory()
    
    def tearDown(self):
        # Clear cache after each test
        cache.clear()

    def test_funding_round_choices_view(self):
        """Test the FundingRoundChoicesView with both cached and uncached requests"""
        # Ensure the cache is empty
        cache.delete('funding_round_choices')
        
        # Set up the view
        view = FundingRoundChoicesView.as_view()
        
        # First request (should hit the db)
        request = self.factory.get('/api/funding-round-choices/')
        response = view(request)
        self.assertEqual(response.status_code, 200)
        
        data = json.loads(response.content.decode('utf-8'))
        expected_data = Organization.get_funding_round_choices()
        self.assertEqual(data, expected_data)
        
        # Second request (should hit cache)
        request = self.factory.get('/api/funding-round-choices/')
        response = view(request)
        self.assertEqual(response.status_code, 200)
        
        # Should still match expected data
        data = json.loads(response.content.decode('utf-8'))
        self.assertEqual(data, expected_data)
    
    def test_rto_policy_choices_view(self):
        """Test the RTOPolicyChoicesView with both cached and uncached requests"""
        # Ensure the cache is empty
        cache.delete('rto_policy_choices')  # Updated cache key
        
        # Set up the view
        view = RTOPolicyChoicesView.as_view()
        
        # First request (should hit the db)
        request = self.factory.get('/api/rto-policy-choices/')
        response = view(request)
        self.assertEqual(response.status_code, 200)
        
        data = json.loads(response.content.decode('utf-8'))
        expected_data = Organization.get_rto_policy_choices()
        self.assertEqual(data, expected_data)
        
        # Second request (should hit cache)
        request = self.factory.get('/api/rto-policy-choices/')
        response = view(request)
        self.assertEqual(response.status_code, 200)
        
        # Should still match expected data
        data = json.loads(response.content.decode('utf-8'))
        self.assertEqual(data, expected_data)
    
    def test_integration_funding_round_choices(self):
        """Test the full API endpoint for funding round choices"""
        # Ensure the cache is empty
        cache.delete('funding_round_choices')
        
        # First request (should hit the db)
        response1 = self.client.get('/api/funding-round-choices/')
        self.assertEqual(response1.status_code, 200)
        data1 = json.loads(response1.content)
        
        # Second request (should hit cache)
        response2 = self.client.get('/api/funding-round-choices/')
        self.assertEqual(response2.status_code, 200)
        data2 = json.loads(response2.content)
        
        # Both responses should match and contain the expected data
        self.assertEqual(data1, data2)
        self.assertIn('S', data1)
        self.assertEqual(data1['S'], 'Seed')
    
    def test_integration_rto_policy_choices(self):
        """Test the full API endpoint for RTO policy choices"""
        # Ensure the cache is empty
        cache.delete('rto_policy_choices')  # Updated cache key
        
        # First request (should hit the db)
        response1 = self.client.get('/api/rto-policy-choices/')
        self.assertEqual(response1.status_code, 200)
        data1 = json.loads(response1.content)
        
        # Second request (should hit cache)
        response2 = self.client.get('/api/rto-policy-choices/')
        self.assertEqual(response2.status_code, 200)
        data2 = json.loads(response2.content)
        
        # Both responses should match and contain the expected data
        self.assertEqual(data1, data2)
        self.assertIn('R', data1)
        self.assertEqual(data1['R'], 'Remote')

    def test_organization_scores_excludes_superseded_rows_and_inactive_types(self):
        """The company detail modal reads only active scores of active types
        (issue #461), consistent with rankings."""
        source = Organization.objects.create(name="Source Org", gives_ratings=True)
        target = Organization.objects.create(name="Detail Org")
        active_type = ScoreType.objects.create(name="Culture")
        retired_type = ScoreType.objects.create(name="Legacy", status=0)
        Score.objects.create(source=source, target=target, type=active_type, score=1.0, status=0)
        Score.objects.create(source=source, target=target, type=active_type, score=5.0)
        Score.objects.create(source=source, target=target, type=retired_type, score=4.0)

        response = self.client.get(
            reverse('organization-scores', kwargs={'pk': target.pk})
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content.decode('utf-8'))
        # The historical 1.0 and the retired-type 4.0 must not average in.
        self.assertEqual(data, [{"type__name": "Culture", "avg_score": 5.0}])

    def _provenance(self, organization):
        response = self.client.get(f'/api/organizations/{organization.pk}/provenance/')
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content.decode('utf-8'))

    def _observation(self, organization, *, status, observed_at=None):
        return CompanyProfileObservation.objects.create(
            organization=organization,
            source_url='https://jobs.example.test/about',
            observed_domain='example.test',
            observed_name='Example Labs',
            locations=['Remote'],
            rto_evidence='Remote first',
            funding_evidence='Series A',
            public_status_evidence='Private company',
            observed_at=observed_at or timezone.now(),
            extraction_version='firecrawl-company-profile.v1',
            status=status,
            fingerprint=f'fp-{status}-{(observed_at or timezone.now()).timestamp()}',
        )

    def test_provenance_without_evidence_lists_all_keys_unverified(self):
        organization = Organization.objects.create(
            name='Detail Org', url='https://example.test', status=1
        )

        data = self._provenance(organization)

        self.assertEqual(data['fields'], [])
        self.assertEqual(
            set(data['unverified_fields']),
            set(CompanyFieldEvidence.FieldKey.values),
        )
        self.assertIsNone(data['latest_observation'])

    def test_provenance_with_accepted_evidence_includes_field_details(self):
        organization = Organization.objects.create(
            name='Detail Org', url='https://example.test', status=1
        )
        observation = self._observation(
            organization, status=CompanyProfileObservation.Status.AUTO_APPLIED
        )
        accept_observation_fields(observation)

        data = self._provenance(organization)

        by_key = {entry['field_key']: entry for entry in data['fields']}
        self.assertIn('rto_policy', by_key)
        entry = by_key['rto_policy']
        self.assertEqual(entry['state'], 'accepted')
        self.assertEqual(entry['value'], 'Remote first')
        # Attribution is the host actually fetched, not the domain the page
        # claims for itself — that is carried in scope, as a claim.
        self.assertEqual(entry['source_domain'], 'jobs.example.test')
        self.assertIsNotNone(entry['observed_at'])
        self.assertEqual(entry['scope'], {'claimed_domain': 'example.test'})
        for key in ('last_checked_at', 'last_successful_fetch_at',
                    'last_changed_at', 'last_verified_at'):
            self.assertIsNotNone(entry[key])
        self.assertFalse(entry['stale'])
        self.assertNotIn('rto_policy', data['unverified_fields'])
        self.assertTrue(data['latest_observation']['is_verified'])

    def test_provenance_pending_observation_is_not_verified(self):
        organization = Organization.objects.create(
            name='Detail Org', url='https://example.test', status=1
        )
        self._observation(
            organization, status=CompanyProfileObservation.Status.PENDING
        )

        data = self._provenance(organization)

        self.assertEqual(data['fields'], [])
        self.assertIn('rto_policy', data['unverified_fields'])
        self.assertEqual(
            data['latest_observation']['status'],
            CompanyProfileObservation.Status.PENDING,
        )
        self.assertFalse(data['latest_observation']['is_verified'])

    def test_provenance_marks_stale_evidence(self):
        organization = Organization.objects.create(
            name='Detail Org', url='https://example.test', status=1
        )
        CompanyFieldEvidence.objects.create(
            organization=organization,
            field_key=CompanyFieldEvidence.FieldKey.RTO_POLICY,
            value_text='Remote first',
            source_url='https://jobs.example.test/about',
            source_domain='example.test',
            observed_at=timezone.now() - timedelta(days=400),
            validation_version='v1',
            extractor_version='v1',
            state=CompanyFieldEvidence.State.ACCEPTED,
            last_verified_at=timezone.now()
            - timedelta(
                days=FIELD_FRESHNESS_POLICY[
                    CompanyFieldEvidence.FieldKey.RTO_POLICY
                ] + 10
            ),
        )

        data = self._provenance(organization)

        entry = data['fields'][0]
        self.assertEqual(entry['field_key'], 'rto_policy')
        self.assertTrue(entry['stale'])

    def test_provenance_response_is_cached(self):
        organization = Organization.objects.create(
            name='Detail Org', url='https://example.test', status=1
        )

        first = self._provenance(organization)
        # A change after the first read is invisible while the cache entry
        # lives: the second response is the cached payload.
        observation = self._observation(
            organization, status=CompanyProfileObservation.Status.AUTO_APPLIED
        )
        accept_observation_fields(observation)
        second = self._provenance(organization)

        self.assertEqual(first, second)
        self.assertEqual(second['fields'], [])

    def test_publication_event_invalidates_provenance_cache(self):
        organization = Organization.objects.create(
            name='Detail Org', url='https://example.test', status=1
        )
        self._provenance(organization)
        cache_key = organization_provenance_api_cache_key(organization.pk)
        self.assertIsNotNone(cache.get(cache_key))

        event = PublicationEvent.objects.create(
            target_type=PublicationEvent.TargetType.ORGANIZATION,
            target_id=organization.pk,
            event_kind=PublicationEvent.EventKind.OBSERVED,
        )
        keys = affected_keys(event)
        self.assertIn(cache_key, keys)
        # The outbox sweep deletes exactly those keys; simulate the sweep.
        cache.delete_many(keys)
        self.assertIsNone(cache.get(cache_key))

        refreshed = self._provenance(organization)
        self.assertIn('unverified_fields', refreshed)

    def test_pre_deploy_cached_payload_without_fields_is_rebuilt(self):
        organization = Organization.objects.create(
            name='Detail Org', url='https://example.test', status=1
        )
        observation = self._observation(
            organization, status=CompanyProfileObservation.Status.AUTO_APPLIED
        )
        accept_observation_fields(observation)
        # An entry written before this deploy: same key, older payload shape
        # with no evidence arrays. Serving it would render the modal with no
        # evidence section at all for the rest of the TTL.
        cache.set(
            organization_provenance_api_cache_key(organization.pk),
            {
                'organization_id': organization.pk,
                'organization_modified': None,
                'organization_created': None,
                'latest_observation': None,
            },
        )

        data = self._provenance(organization)

        self.assertIn('fields', data)
        self.assertIn(
            'rto_policy', {entry['field_key'] for entry in data['fields']}
        )
        self.assertIsNotNone(data['latest_observation'])
