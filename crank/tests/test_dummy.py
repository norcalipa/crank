# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.test import TestCase


class SanityTests(TestCase):

    def test_sanity(self):
        localbool = True
        self.assertIs(localbool, True)
