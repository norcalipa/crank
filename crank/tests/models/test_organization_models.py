from django.test import TestCase
from crank.models.organization import Organization


class OrganizationModelTests(TestCase):
    def test_organization_creation(self):
        org_name = "Test Organization"
        org = Organization.objects.create(name=org_name)
        self.assertEqual(org.name, org_name)

    def test_organization_defaults(self):
        org_name = "Test Organization"
        org = Organization.objects.create(name=org_name)
        self.assertIs(org.public, True)
        self.assertEqual(org.type, Organization.Type.COMPANY)
        self.assertEqual(org.funding_round, Organization.FundingRound.PUBLIC)
