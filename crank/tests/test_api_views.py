# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.test import TestCase, Client, override_settings, RequestFactory
from django.urls import reverse
from django.core.cache import cache
from django.conf import settings
from crank.models.organization import Organization
from crank.views import api
import json
from unittest.mock import patch

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