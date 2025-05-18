# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import json

from django.test import TestCase, Client, RequestFactory, override_settings
from django.urls import reverse
from django.core.cache import cache
from django.conf import settings

from crank.models.organization import Organization
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