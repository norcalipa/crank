# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.test import TestCase
from crank.models.organization import Organization
from crank.models.score import ScoreType, Score


class OrganizationModelTests(TestCase):
    def test_organization_creation(self):
        org_name = "Test Organization"
        org = Organization.objects.create(name=org_name)
        self.assertEqual(org.name, org_name)
        self.assertEqual(org.name, str(org))

    def test_organization_defaults(self):
        org_name = "Test Organization"
        org = Organization.objects.create(name=org_name)
        self.assertIs(org.public, True)
        self.assertEqual(org.type, Organization.Type.COMPANY)
        self.assertEqual(org.funding_round, Organization.FundingRound.PUBLIC)

    def test_avg_scores(self):
        score_type = ScoreType.objects.create(name="Test Score Type")
        score_type2 = ScoreType.objects.create(name="Test Score Type 2")
        source_org = Organization.objects.create(name="Source Organization")
        source_org2 = Organization.objects.create(name="Source Organization 2")
        target_org = Organization.objects.create(name="Target Organization")
        Score.objects.create(type=score_type, source=source_org, target=target_org, score=3.0)
        Score.objects.create(type=score_type, source=source_org2, target=target_org, score=5.0)
        Score.objects.create(type=score_type2, source=source_org, target=target_org, score=1.0)
        Score.objects.create(type=score_type2, source=source_org2, target=target_org, score=2.0)

        score = target_org.avg_scores()

        self.assertEqual(score[0]['avg_score'], 4.0)
        self.assertEqual(score[1]['avg_score'], 1.5)
