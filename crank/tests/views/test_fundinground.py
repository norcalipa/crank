# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import json
from django.test import TestCase, RequestFactory
from crank.views.fundinground import FundingRoundChoicesView
from crank.models.organization import Organization


class FundingRoundViewTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.view = FundingRoundChoicesView.as_view()

    def test_get_funding_rounds(self):
        # Mock the get_funding_round_choices method
        #Organization.get_funding_round_choices = lambda: Organization.get_funding_round_choices()

        request = self.factory.get('/fundingrounds')
        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), Organization.get_funding_round_choices())
