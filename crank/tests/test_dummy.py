from django.test import TestCase


class SanityTests(TestCase):

    def test_sanity(self):
        localbool = True
        self.assertIs(localbool, True)
