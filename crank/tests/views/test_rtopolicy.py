import json
from django.test import TestCase, RequestFactory
from crank.views.rtopolicy import RTOPolicyChoicesView
from crank.models.organization import Organization


class RTOPolicyChoicesViewTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.view = RTOPolicyChoicesView.as_view()

    def test_get_rto_policy_choices(self):
        # Mock the get_rto_policy_choices method
        Organization.get_rto_policy_choices = lambda: {'policy1': 'description1', 'policy2': 'description2'}

        request = self.factory.get('/rtopolicychoices')
        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {'policy1': 'description1', 'policy2': 'description2'})