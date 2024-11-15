# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import json
from datetime import datetime

from django.contrib.sessions.middleware import SessionMiddleware
from django.core.serializers import serialize
from django.test import TestCase, Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils.html import escape
from unittest.mock import patch

from crank.models.organization import Organization
from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site

from crank.models.score import Score, ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight
from crank.views.index import IndexView
from crank.settings import DEFAULT_ALGORITHM_ID
from django.core.cache import cache


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class IndexViewTests(TestCase):

    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.client = Client()
        self.view = IndexView.as_view()
        self.index_url = reverse('index')  # replace 'index' with the actual name of the IndexView in your urls.py
        ScoreAlgorithm.objects.create(id=DEFAULT_ALGORITHM_ID, name='Test Algorithm',
                                                          description_content='test.md', status=1)
        self.algorithms = ScoreAlgorithm.objects.filter(status=1)
        cache.set('algorithm_object_list', self.algorithms)  # Set the cache

        social_app = SocialApp.objects.create(
            provider='google',
            name='Google',
            client_id='test',
            secret='test',
        )
        social_app.sites.add(Site.objects.get_current())
        cache.set('social_app_google', social_app)  # Set the cache

    def setup_scores(self):
        # Create some test data
        self.organization1 = Organization.objects.create(
            name="Org 1", funding_round="P", rto_policy="R", accelerated_vesting=True, url='org1.com',
            gives_ratings=True, public=True, type="C",
        )
        self.organization2 = Organization.objects.create(
            name="Org 2", funding_round="B", rto_policy="H", accelerated_vesting=False, url='org2.com',
            gives_ratings=False, public=False, type="C",
        )

        score_type = ScoreType.objects.create(id=1, name='Test Score Type')
        ScoreAlgorithmWeight.objects.create(algorithm_id=DEFAULT_ALGORITHM_ID,
                                            type_id=score_type.id, weight=1.0)
        Score.objects.create(source_id=self.organization1.id, target_id=self.organization1.id, score=5.0,
                             type_id=score_type.id)
        Score.objects.create(source_id=self.organization2.id, target_id=self.organization2.id, score=1.0,
                             type_id=score_type.id)

    def assertOrgValues(self, expected, actual, checkExtended=False):
        self.assertEqual(actual['accelerated_vesting'], expected['accelerated_vesting'])
        if checkExtended:
            self.assertIsNotNone(actual['id'])
            self.assertEqual(actual['avg_score'], 5.0)
            self.assertEqual(actual['ranking'], 1)
            self.assertEqual(actual['profile_completeness'], 100.0)
        self.assertEqual(actual['accelerated_vesting'], expected['accelerated_vesting'])
        self.assertEqual(actual['funding_round'], expected['funding_round'])
        self.assertEqual(actual['name'], expected['name'])
        self.assertEqual(actual['rto_policy'], expected['rto_policy'])
        self.assertEqual(actual['type'], expected['type'])

    def add_session_to_request(self, request):
        middleware = SessionMiddleware(lambda req: None)
        middleware.process_request(request)
        request.session.save()

    def test_index_view_renders_organization_list(self):
        self.setup_scores()
        response = self.client.get(self.index_url)

        self.assertEqual(response.status_code, 200)
        orgList = response.context_data['top_organization_list']
        self.assertEqual(orgList[0]['name'], escape(self.organization1.name))
        self.assertEqual(orgList[1]['name'], escape(self.organization2.name))

    def test_index_view(self):
        request = self.factory.get(self.index_url)
        self.add_session_to_request(request)
        request.session['algorithm_id'] = '1'  # Set algorithm_id in session

        self.setup_scores()
        serialized_org = json.loads(serialize('json', [self.organization1]))[0]['fields']
        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response, 'Test Algorithm')  # contents of the test.md file
        self.assertOrgValues(response.context_data["top_organization_list"][0], serialized_org)

    def test_index_view_with_algo(self):
        self.setup_scores()
        algourl = self.index_url + 'algo/1/'
        response = self.client.get(algourl)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response, 'Test Algorithm')  # contents of the test.md file
        serialized_org = json.loads(serialize('json', [self.organization1]))[0]['fields']
        self.assertOrgValues(response.context_data["top_organization_list"][0], serialized_org)

    def test_index_view_with_empty_algorithm(self):
        # Ensure no algorithms exist
        ScoreAlgorithm.objects.all().delete()

        request = self.factory.get(self.index_url)
        self.add_session_to_request(request)
        request.session['algorithm_id'] = '1'  # Set a non-existent algorithm_id in session

        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response,
                            'No organizations are available or the Score Algorithm you specified doesn\'t exist.')
        self.assertEqual(response.context_data["top_organization_list"], [])

    def test_index_view_with_bad_algo(self):
        self.setup_scores()
        algourl = self.index_url + 'algo/99999/'
        response = self.client.get(algourl)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response, 'Test Algorithm')  # contents of the test.md file
        serialized_org = json.loads(serialize('json', [self.organization1]))[0]['fields']
        self.assertOrgValues(response.context_data["top_organization_list"][0], serialized_org)

    def test_empty_index_view(self):
        # we aren't adding data, so there should be no results
        request = self.factory.get(self.index_url)
        self.add_session_to_request(request)
        request.session['algorithm_id'] = '1'  # Set algorithm_id in session

        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response,
                            "No organizations are available or the Score Algorithm you specified doesn't exist.")
        self.assertEqual(response.context_data["top_organization_list"], [])

    def test_index_get_queryset(self):
        request = self.factory.get(self.index_url)
        self.add_session_to_request(request)
        request.session['algorithm_id'] = '1'  # Set algorithm_id in session

        # Create an IndexView instance
        index_view = IndexView()
        index_view.request = request
        self.setup_scores()

        # Call get_queryset() and check the returned queryset
        queryset = index_view.get_queryset()
        serialized_org = json.loads(serialize('json', [self.organization1]))[0]['fields']
        self.assertOrgValues(serialized_org, queryset[0], True)

    @patch('crank.models.organization.Organization.objects.filter')
    def test_index_view_organization_does_not_exist(self, mock_filter):
        # Mock the filter method to raise Organization.DoesNotExist
        mock_filter.side_effect = Organization.DoesNotExist

        request = self.factory.get(self.index_url)
        self.add_session_to_request(request)
        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response,
                            'No organizations are available or the Score Algorithm you specified doesn\'t exist.')
        self.assertEqual(response.context_data["top_organization_list"], [])

    def test_post_method_valid_data(self):
        # Note that accelerated_vesting is a toggle, so it should be set to the opposite of the current value after a post request
        form_data = {
            'accelerated_vesting': False,
            # Add other form fields as necessary
        }
        request = self.factory.post(self.index_url, data=form_data)
        self.add_session_to_request(request)
        request.session['accelerated_vesting'] = True

        response = self.view(request)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(request.session.get('accelerated_vesting'))

    def test_post_method_invalid_data(self):
        # Note that accelerated_vesting is a toggle, so it should be set to the opposite of the current value after a post request
        form_data = {
            'accelerated_vesting': 'invalid_value',  # Invalid data
        }
        request = self.factory.post(self.index_url, data=form_data)
        self.add_session_to_request(request)  # Ensure the request has a session
        request.session['accelerated_vesting'] = False

        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(request.session.get('accelerated_vesting'))
