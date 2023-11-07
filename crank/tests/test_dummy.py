from django.test import TestCase

from crank.models.organization import *


class OrganizationTests(TestCase):

    def test_sanity(self):
        localbool = True
        self.assertIs(localbool, True)

    def test_organization_defaults(self):
        org_name = "Test"
        org = Organization(name=org_name)

        self.assertIs(org.public, True)
        self.assertIs(org.name, org_name)
        self.assertIs(org.type, Organization.Type.COMPANY)
        self.assertIs(org.funding_round, Organization.FundingRound.PUBLIC)
