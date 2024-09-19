import json
from datetime import datetime

from django.core.serializers import serialize
from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.utils.html import escape

from crank.models.organization import Organization
from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site

from crank.models.score import Score, ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight
from crank.views.index import IndexView
from crank.settings import DEFAULT_ALGORITHM_ID


class IndexViewTests(TestCase):

    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client()
        self.index_url = reverse('index')  # replace 'index' with the actual name of the IndexView in your urls.py

        # Create a SocialApp object for testing
        self.social_app = SocialApp.objects.create(
            provider='google',
            name='Google',
            client_id='test',
            secret='test',
        )
        self.score_algorithm = ScoreAlgorithm.objects.create(id=DEFAULT_ALGORITHM_ID, name='Test Algorithm',
                                                             description_content='test.md')
        self.social_app.sites.add(Site.objects.get_current())

        self.date = datetime(2024, 1, 1)
        # Create some test data
        self.organization1 = Organization.objects.create(
            accelerated_vesting=True,
            activate_date=self.date,
            created=self.date,
            deactivate_date=None,
            funding_round="A",
            gives_ratings=False,
            modified=self.date,
            name="Org 1",
            public=False,
            rto_policy="R",
            type="C",
        )
        self.organization2 = Organization.objects.create(
            name="Org 2", funding_round="B", rto_policy="H", accelerated_vesting=False, url='org2.com'
        )

    def setup_scores(self):
        org = Organization.objects.create(
            accelerated_vesting=False,
            funding_round='P',
            id=3,
            name='Test Organization',
            rto_policy='H',
            type='C')
        score_type = ScoreType.objects.create(id=1, name='Test Score Type')
        ScoreAlgorithmWeight.objects.create(algorithm_id=self.score_algorithm.id,
                                            type_id=score_type.id, weight=1.0)
        Score.objects.create(source_id=org.id, target_id=org.id, score=1.0, type_id=score_type.id)
        return org

    def test_index_view_renders_organization_list(self):
        response = self.client.get(reverse('index'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, escape(self.organization1.name))
        self.assertContains(response, escape(self.organization2.name))

    def test_index_view(self):
        request = self.factory.get(self.index_url)
        request.session = {'algorithm_id': '1'}  # Set algorithm_id in session

        org = self.setup_scores()
        response = self.client.get(self.index_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response, 'Test Algorithm')  # contents of the test.md file
        self.assertQuerySetEqual(response.context["top_organization_list"], [org])

    def test_index_view_with_algo(self):
        org = self.setup_scores()
        algourl = self.index_url + 'algo/1/'
        response = self.client.get(algourl)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response, 'Test Algorithm')  # contents of the test.md file
        self.assertQuerySetEqual(response.context["top_organization_list"], [org])

    def test_index_view_with_bad_algo(self):
        org = self.setup_scores()
        algourl = self.index_url + 'algo/99999/'
        response = self.client.get(algourl)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response, 'Test Algorithm')  # contents of the test.md file
        self.assertQuerySetEqual(response.context["top_organization_list"], [org])

    def test_empty_index_view(self):
        # we aren't adding data, so there should be no results
        request = self.factory.get(self.index_url)
        request.session = {'algorithm_id': '1'}  # Set algorithm_id in session

        response = self.client.get(self.index_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response,
                            "No organizations are available or the Score Algorithm you specified doesn't exist.")
        self.assertQuerySetEqual(response.context["top_organization_list"], [])

    def test_index_get_queryset(self):
        request = self.factory.get(self.index_url)
        request.session = {'algorithm_id': '1'}  # Set algorithm_id in session

        # Create an IndexView instance
        index_view = IndexView()
        index_view.request = request
        org = self.setup_scores()

        # Call get_queryset() and check the returned queryset
        queryset = index_view.get_queryset()
        serialized_org = json.loads(serialize('json', [org]))[0]['fields']
        self.assertQuerySetEqual(queryset, [serialized_org])
