# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Rankings revision and the two public rankings endpoints (issue #485c)."""
import copy
import json
import re

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from crank.models.organization import Organization
from crank.models.score import Score, ScoreAlgorithm, ScoreAlgorithmWeight, ScoreType
from crank.services.scores import algorithm_results_cache_key
from crank.settings import DEFAULT_ALGORITHM_ID
from crank.views.index import ranking_rows, rankings_revision

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

SAMPLE_ROWS = [
    {
        'id': 1, 'name': 'Alpha', 'type': 'C', 'rto_policy': 'R', 'funding_round': 'P',
        'accelerated_vesting': True, 'avg_score': 4.8, 'profile_completeness': 100.0, 'ranking': 1,
        'rating_dimensions_total': 2, 'rating_dimensions_covered': 2,
        'evidence': {
            'verified': 3, 'stale': 1, 'unknown': 2, 'total': 6, 'fact_coverage': 4,
            'last_verified_at': '2026-10-01T00:00:00+00:00', 'pending_review': 0,
        },
    },
    {
        'id': 2, 'name': 'Beta', 'type': 'C', 'rto_policy': 'H', 'funding_round': 'B',
        'accelerated_vesting': False, 'avg_score': 4.2, 'profile_completeness': 50.0, 'ranking': 2,
        'rating_dimensions_total': 2, 'rating_dimensions_covered': 1,
        'evidence': {
            'verified': 0, 'stale': 0, 'unknown': 6, 'total': 6, 'fact_coverage': 0,
            'last_verified_at': None, 'pending_review': 1,
        },
    },
]


class RankingsRevisionTests(TestCase):
    def test_revision_is_sixteen_hex_characters_and_stable(self):
        revision = rankings_revision(SAMPLE_ROWS)
        self.assertRegex(revision, r'^[0-9a-f]{16}$')
        self.assertEqual(revision, rankings_revision(copy.deepcopy(SAMPLE_ROWS)))
        self.assertEqual(revision, rankings_revision(tuple(SAMPLE_ROWS)))

    def test_scores_are_digested_as_the_list_shows_them(self):
        rows = copy.deepcopy(SAMPLE_ROWS)
        rows[0]['avg_score'] = 4.8040001
        self.assertEqual(rankings_revision(rows), rankings_revision(SAMPLE_ROWS))
        rows[0]['avg_score'] = 4.81
        self.assertNotEqual(rankings_revision(rows), rankings_revision(SAMPLE_ROWS))
        rows[0]['avg_score'] = None  # a row with no score still has a revision
        self.assertRegex(rankings_revision(rows), r'^[0-9a-f]{16}$')

    def test_every_visible_field_changes_the_revision(self):
        base = rankings_revision(SAMPLE_ROWS)
        changes = {
            'id': 9, 'name': 'Alpha 2', 'rto_policy': 'O', 'funding_round': 'A',
            'accelerated_vesting': False, 'avg_score': 3.0, 'ranking': 3,
            'rating_dimensions_total': 3, 'rating_dimensions_covered': 1,
        }
        for field, value in changes.items():
            rows = copy.deepcopy(SAMPLE_ROWS)
            rows[0][field] = value
            self.assertNotEqual(rankings_revision(rows), base, field)
        evidence_changes = {
            'verified': 4, 'stale': 0, 'unknown': 1,
            'last_verified_at': '2026-10-02T00:00:00+00:00',
        }
        for field, value in evidence_changes.items():
            rows = copy.deepcopy(SAMPLE_ROWS)
            rows[0]['evidence'][field] = value
            self.assertNotEqual(rankings_revision(rows), base, f'evidence.{field}')
        rows = copy.deepcopy(SAMPLE_ROWS)
        rows[1]['evidence']['pending_review'] = 0
        self.assertNotEqual(rankings_revision(rows), base, 'pending_review appears or goes')
        rows = copy.deepcopy(SAMPLE_ROWS)
        rows[1]['evidence']['last_verified_at'] = '2026-10-01T00:00:00+00:00'
        self.assertNotEqual(rankings_revision(rows), base, 'a first verification date')

    def test_changes_the_list_does_not_show_leave_the_revision_alone(self):
        base = rankings_revision(SAMPLE_ROWS)
        quiet = [
            ('type', lambda row: row.update(type='S')),
            ('profile_completeness', lambda row: row.update(profile_completeness=90.0)),
            ('fact_coverage', lambda row: row['evidence'].update(fact_coverage=5)),
            ('evidence.total', lambda row: row['evidence'].update(total=7)),
            ('same-day re-verification',
             lambda row: row['evidence'].update(last_verified_at='2026-10-01T17:45:09.123456+00:00')),
        ]
        for label, change in quiet:
            rows = copy.deepcopy(SAMPLE_ROWS)
            change(rows[0])
            self.assertEqual(rankings_revision(rows), base, label)
        rows = copy.deepcopy(SAMPLE_ROWS)
        rows[1]['evidence']['pending_review'] = 3
        self.assertEqual(rankings_revision(rows), base, 'open claims are a badge, not a count')

    def test_order_changes_the_revision(self):
        self.assertNotEqual(rankings_revision(SAMPLE_ROWS), rankings_revision(list(reversed(SAMPLE_ROWS))))

    def test_empty_list_has_a_revision(self):
        self.assertRegex(rankings_revision([]), r'^[0-9a-f]{16}$')


@override_settings(CACHES=LOCMEM)
class RankingsApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.algorithm = ScoreAlgorithm.objects.create(
            id=DEFAULT_ALGORITHM_ID, name='Test Algorithm', description_content='test.md', status=1)
        self.inactive = ScoreAlgorithm.objects.create(
            id=DEFAULT_ALGORITHM_ID + 1, name='Retired', description_content='test.md', status=0)
        cache.set('algorithm_object_list', ScoreAlgorithm.objects.filter(status=1))
        score_type = ScoreType.objects.create(id=1, name='Test Score Type')
        ScoreAlgorithmWeight.objects.create(
            algorithm_id=DEFAULT_ALGORITHM_ID, type_id=score_type.id, weight=1.0)
        self.score_type = score_type
        self.orgs = [self.make_org(name, score) for name, score in (('Org 1', 5.0), ('Org 2', 1.0))]
        self.revision_url = reverse('rankings-revision', args=[DEFAULT_ALGORITHM_ID])
        self.list_url = reverse('rankings-list', args=[DEFAULT_ALGORITHM_ID])

    def make_org(self, name, score):
        org = Organization.objects.create(
            name=name, funding_round='P', rto_policy='R', accelerated_vesting=True, url=f'{name}.com'.replace(' ', ''),
            gives_ratings=True, public=True, type='C')
        Score.objects.create(source_id=org.id, target_id=org.id, score=score, type_id=self.score_type.id)
        return org

    @staticmethod
    def embedded(response):
        match = re.search(
            r'<script id="organization-data" type="application/json">(.*?)</script>',
            response.content.decode(), re.S)
        return json.loads(match.group(1))

    @staticmethod
    def config_revision(response):
        return re.search(r'data-revision="([^"]*)"', response.content.decode()).group(1)

    def test_page_and_api_agree_for_both_routes(self):
        api_rows = self.client.get(self.list_url).json()
        poll = self.client.get(self.revision_url).json()
        self.assertEqual(api_rows['revision'], poll['revision'])
        for url in ('/', f'/algo/{DEFAULT_ALGORITHM_ID}/'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.config_revision(response), api_rows['revision'], url)
            self.assertEqual(self.embedded(response), api_rows['organizations'], url)
        self.assertEqual([row['name'] for row in api_rows['organizations']], ['Org 1', 'Org 2'])

    def test_cached_algo_page_embeds_revision_with_its_rows(self):
        first = self.client.get(f'/algo/{DEFAULT_ALGORITHM_ID}/')
        revision = self.config_revision(first)
        Score.objects.filter(target_id=self.orgs[1].id).update(score=5.0)
        cache.delete(algorithm_results_cache_key(DEFAULT_ALGORITHM_ID))
        # The page is still the cached one: rows and revision still agree...
        second = self.client.get(f'/algo/{DEFAULT_ALGORITHM_ID}/')
        self.assertEqual(self.config_revision(second), revision)
        self.assertEqual(rankings_revision(self.embedded(second)), revision)
        # ...while the endpoint reads the rows as they are now (AC 17).
        self.assertNotEqual(self.client.get(self.revision_url).json()['revision'], revision)

    def test_revision_survives_a_cache_rebuild_and_follows_a_score_change(self):
        before = self.client.get(self.revision_url).json()['revision']
        cache.clear()
        cache.set('algorithm_object_list', ScoreAlgorithm.objects.filter(status=1))
        self.assertEqual(self.client.get(self.revision_url).json()['revision'], before)
        Score.objects.filter(target_id=self.orgs[1].id).update(score=2.0)
        cache.clear()
        cache.set('algorithm_object_list', ScoreAlgorithm.objects.filter(status=1))
        self.assertNotEqual(self.client.get(self.revision_url).json()['revision'], before)

    def test_same_bytes_for_anonymous_and_signed_in(self):
        anonymous = [self.client.get(url) for url in (self.revision_url, self.list_url)]
        self.client.force_login(User.objects.create_user('rank-reader', 'rank@example.com', 'pw12345'))
        signed_in = [self.client.get(url) for url in (self.revision_url, self.list_url)]
        for before, after in zip(anonymous, signed_in):
            self.assertEqual(before.status_code, 200)
            self.assertEqual(before.content, after.content)

    def test_unknown_and_inactive_algorithms_are_404_without_fallback(self):
        for algorithm_id in (9999, self.inactive.id):
            for name in ('rankings-revision', 'rankings-list'):
                response = self.client.get(reverse(name, args=[algorithm_id]))
                self.assertEqual(response.status_code, 404, (name, algorithm_id))
                self.assertEqual(response['Cache-Control'], 'no-store, private')
                self.assertNotIn('organizations', response.json())
                self.assertNotIn('revision', response.json())

    def test_only_get_is_answered_and_every_answer_is_never_stored(self):
        for name in ('rankings-revision', 'rankings-list'):
            url = reverse(name, args=[DEFAULT_ALGORITHM_ID])
            ok = self.client.get(url)
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok['Cache-Control'], 'no-store, private')
            for method in ('post', 'put', 'delete', 'patch'):
                refused = getattr(self.client, method)(url)
                self.assertEqual(refused.status_code, 405, (name, method))
                self.assertEqual(refused['Cache-Control'], 'no-store, private')

    def test_the_session_is_never_written(self):
        for url in (self.revision_url, self.list_url):
            response = self.client.get(url)
            self.assertNotIn('sessionid', response.cookies)
        self.assertEqual(dict(self.client.session), {})

    def test_a_cache_hit_costs_one_query(self):
        self.client.get(self.list_url)  # prime the results entry
        with self.assertNumQueries(1):
            self.client.get(self.revision_url)
        with self.assertNumQueries(1):
            self.client.get(self.list_url)

    def test_poll_after_seconds_follows_the_shared_setting(self):
        self.assertEqual(self.client.get(self.revision_url).json()['poll_after_seconds'], 60)
        with override_settings(RESULT_REVISION_POLL_SECONDS=90):
            self.assertEqual(self.client.get(self.revision_url).json()['poll_after_seconds'], 90)
        with override_settings(RESULT_REVISION_POLL_SECONDS=0):
            self.assertIsNone(self.client.get(self.revision_url).json()['poll_after_seconds'])

    def test_ties_are_ordered_by_id(self):
        tied = [self.make_org(f'Tie {n}', 3.0) for n in range(3)]
        rows = ranking_rows(DEFAULT_ALGORITHM_ID)
        tie_ids = [row['id'] for row in rows if row['id'] in {org.id for org in tied}]
        self.assertEqual(len({row['ranking'] for row in rows if row['id'] in tie_ids}), 1)
        self.assertEqual(tie_ids, sorted(tie_ids))
        ranks = [(row['ranking'], row['id']) for row in rows]
        self.assertEqual(ranks, sorted(ranks))
