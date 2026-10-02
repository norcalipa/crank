# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the compact timestamp filter used on the Job Retrieval Operations page (issue #481)."""

from django.test import SimpleTestCase

from crank.templatetags.jro_time import jro_time


class JroTimeFilterTests(SimpleTestCase):
    def test_drops_seconds_and_microseconds_and_keeps_full_iso(self):
        iso = "2026-09-29T21:03:33.081271+00:00"
        html = jro_time(iso)
        self.assertIn(">2026-09-29 21:03 UTC</time>", html)
        self.assertIn(f'datetime="{iso}"', html)
        self.assertIn(f'title="{iso}"', html)
        self.assertNotIn("081271</time>", html)

    def test_converts_offsets_to_utc(self):
        self.assertIn(">2026-09-29 21:03 UTC<", jro_time("2026-09-29T14:03:00-07:00"))

    def test_naive_timestamp_is_labeled_utc_without_conversion(self):
        self.assertIn(">2026-09-29 21:03 UTC<", jro_time("2026-09-29T21:03:00"))

    def test_empty_values_render_dash(self):
        self.assertEqual(jro_time(None), "—")
        self.assertEqual(jro_time(""), "—")

    def test_unparseable_value_is_escaped_not_dropped(self):
        self.assertEqual(jro_time("<b>oops</b>"), "&lt;b&gt;oops&lt;/b&gt;")
