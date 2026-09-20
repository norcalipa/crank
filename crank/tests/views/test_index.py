# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import json
from datetime import datetime

from django.contrib.auth.models import User
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.serializers import serialize
from django.test import TestCase, Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils.html import escape
from unittest.mock import patch

from crank.models.organization import Organization
from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site

from crank.models.score import Score, ScoreType, ScoreAlgorithm, ScoreAlgorithmWeight
from crank.views.index import COMPANY_ID_PLACEHOLDER, IndexView
from crank.settings import DEFAULT_ALGORITHM_ID
from crank.auth import SESSION_EXPIRED_MESSAGE
from crank.services.scores import SCORE_CACHE_KEY_VERSION, algorithm_results_cache_key
from django.core.cache import cache


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class IndexViewTests(TestCase):

    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.client = Client()
        self.view = IndexView.as_view()
        self.index_url = reverse('index')  # replace 'index' with the actual name of the IndexView in your urls.py
        ScoreAlgorithm.objects.create(id=DEFAULT_ALGORITHM_ID, name='Test Algorithm',
                                                          description_content='test.md', status=1)
        self.algorithms = ScoreAlgorithm.objects.filter(status=1)
        cache.set('algorithm_object_list', self.algorithms)  # Set the cache

        social_app = SocialApp.objects.create(
            provider='google',
            name='Google',
            client_id='test',
            secret='test',
        )
        social_app.sites.add(Site.objects.get_current())
        cache.set('social_app_google', social_app)  # Set the cache

    def setup_scores(self):
        # Create some test data
        self.organization1 = Organization.objects.create(
            name="Org 1", funding_round="P", rto_policy="R", accelerated_vesting=True, url='org1.com',
            gives_ratings=True, public=True, type="C",
        )
        self.organization2 = Organization.objects.create(
            name="Org 2", funding_round="B", rto_policy="H", accelerated_vesting=False, url='org2.com',
            gives_ratings=False, public=False, type="C",
        )

        score_type = ScoreType.objects.create(id=1, name='Test Score Type')
        ScoreAlgorithmWeight.objects.create(algorithm_id=DEFAULT_ALGORITHM_ID,
                                            type_id=score_type.id, weight=1.0)
        Score.objects.create(source_id=self.organization1.id, target_id=self.organization1.id, score=5.0,
                             type_id=score_type.id)
        Score.objects.create(source_id=self.organization2.id, target_id=self.organization2.id, score=1.0,
                             type_id=score_type.id)

    def assertOrgValues(self, expected, actual, checkExtended=False):
        self.assertEqual(actual['accelerated_vesting'], expected['accelerated_vesting'])
        if checkExtended:
            self.assertIsNotNone(actual['id'])
            self.assertEqual(actual['avg_score'], 5.0)
            self.assertEqual(actual['ranking'], 1)
            self.assertEqual(actual['profile_completeness'], 100.0)
        self.assertEqual(actual['accelerated_vesting'], expected['accelerated_vesting'])
        self.assertEqual(actual['funding_round'], expected['funding_round'])
        self.assertEqual(actual['name'], expected['name'])
        self.assertEqual(actual['rto_policy'], expected['rto_policy'])
        self.assertEqual(actual['type'], expected['type'])

    def add_session_to_request(self, request):
        middleware = SessionMiddleware(lambda req: None)
        middleware.process_request(request)
        request.session.save()

    def test_index_view_renders_organization_list(self):
        self.setup_scores()
        response = self.client.get(self.index_url)

        self.assertEqual(response.status_code, 200)
        orgList = response.context_data['top_organization_list']
        self.assertEqual(orgList[0]['name'], escape(self.organization1.name))
        self.assertEqual(orgList[1]['name'], escape(self.organization2.name))

    def test_index_view(self):
        request = self.factory.get(self.index_url)
        self.add_session_to_request(request)
        request.session['algorithm_id'] = '1'  # Set algorithm_id in session

        self.setup_scores()
        serialized_org = json.loads(serialize('json', [self.organization1]))[0]['fields']
        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response, 'Test Algorithm')  # contents of the test.md file
        self.assertOrgValues(response.context_data["top_organization_list"][0], serialized_org)

    def test_index_view_with_algo(self):
        self.setup_scores()
        algourl = self.index_url + 'algo/1/'
        response = self.client.get(algourl)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response, 'Test Algorithm')  # contents of the test.md file
        serialized_org = json.loads(serialize('json', [self.organization1]))[0]['fields']
        self.assertOrgValues(response.context_data["top_organization_list"][0], serialized_org)

    def test_index_view_with_empty_algorithm(self):
        # Ensure no algorithms exist
        ScoreAlgorithm.objects.all().delete()

        request = self.factory.get(self.index_url)
        self.add_session_to_request(request)
        request.session['algorithm_id'] = '1'  # Set a non-existent algorithm_id in session

        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response,
                            'No organizations are available or the Score Algorithm you specified doesn\'t exist.')
        self.assertEqual(response.context_data["top_organization_list"], [])

    def test_index_view_with_bad_algo(self):
        self.setup_scores()
        algourl = self.index_url + 'algo/99999/'
        response = self.client.get(algourl)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response, 'Test Algorithm')  # contents of the test.md file
        serialized_org = json.loads(serialize('json', [self.organization1]))[0]['fields']
        self.assertOrgValues(response.context_data["top_organization_list"][0], serialized_org)

    def test_empty_index_view(self):
        # Algorithm exists but no score data – the React component renders
        # the no-results panel client-side, so the server template shows
        # the algorithm card with an empty organization list.
        request = self.factory.get(self.index_url)
        self.add_session_to_request(request)
        request.session['algorithm_id'] = '1'  # Set algorithm_id in session

        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CRank')
        self.assertContains(response, 'organization-list')
        self.assertContains(response, 'organization-list-config')
        self.assertEqual(response.context_data["top_organization_list"], [])

    def test_index_get_queryset(self):
        request = self.factory.get(self.index_url)
        self.add_session_to_request(request)
        request.session['algorithm_id'] = '1'  # Set algorithm_id in session

        # Create an IndexView instance
        index_view = IndexView()
        index_view.request = request
        self.setup_scores()

        # Call get_queryset() and check the returned queryset
        queryset = index_view.get_queryset()
        serialized_org = json.loads(serialize('json', [self.organization1]))[0]['fields']
        self.assertOrgValues(serialized_org, queryset[0], True)

    @patch('crank.models.organization.Organization.objects.filter')
    def test_index_view_organization_does_not_exist(self, mock_filter):
        # Mock the filter method to raise Organization.DoesNotExist
        mock_filter.side_effect = Organization.DoesNotExist

        request = self.factory.get(self.index_url)
        self.add_session_to_request(request)
        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'organization-list')
        self.assertEqual(response.context_data["top_organization_list"], [])

    def test_post_method_valid_data(self):
        # Note that accelerated_vesting is a toggle, so it should be set to the opposite of the current value after a post request
        form_data = {
            'accelerated_vesting': False,
            # Add other form fields as necessary
        }
        request = self.factory.post(self.index_url, data=form_data)
        self.add_session_to_request(request)
        request.session['accelerated_vesting'] = True

        response = self.view(request)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(request.session.get('accelerated_vesting'))

    def test_post_method_invalid_data(self):
        # Note that accelerated_vesting is a toggle, so it should be set to the opposite of the current value after a post request
        form_data = {
            'accelerated_vesting': 'invalid_value',  # Invalid data
        }
        request = self.factory.post(self.index_url, data=form_data)
        self.add_session_to_request(request)  # Ensure the request has a session
        request.session['accelerated_vesting'] = False

        response = self.view(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(request.session.get('accelerated_vesting'))

    def test_template_renders_config_span_unauthenticated(self):
        self.setup_scores()
        response = self.client.get(self.index_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'organization-list-config')
        self.assertContains(response, 'data-can-suggest-company="false"')

    def test_cached_algo_page_identical_across_accounts_and_user_free(self):
        """The full-page-cached algo view serves every account the same
        auth-neutral payload, including across cookie differences (issue
        #470 AC6; review finding: auth-dependent cached shell)."""
        self.setup_scores()
        user_a = User.objects.create_user(
            "cache-user-a", "cache-user-a@example.com", "pw12345"
        )
        user_b = User.objects.create_user(
            "cache-user-b", "cache-user-b@example.com", "pw12345"
        )
        algo_url = f"/algo/{DEFAULT_ALGORITHM_ID}/"
        anonymous = self.client.get(algo_url)
        self.assertEqual(anonymous.status_code, 200)
        self.client.force_login(user_a)
        first = self.client.get(algo_url)
        self.assertEqual(first.status_code, 200)
        self.client.force_login(user_b)
        second = self.client.get(algo_url)
        self.assertEqual(second.status_code, 200)
        # A different session cookie and CSRF cookie must not change the
        # served payload either.
        varied_cookies = self.client.get(
            algo_url,
            HTTP_COOKIE="sessionid=bogus; csrftoken=bogus",
        )
        self.assertEqual(varied_cookies.status_code, 200)
        self.assertEqual(first.content, second.content)
        self.assertEqual(anonymous.content, second.content)
        self.assertEqual(varied_cookies.content, second.content)
        for marker in (
            b"cache-user-a",
            b"cache-user-b",
            b"@example.com",
            b"csrfmiddlewaretoken",
        ):
            self.assertNotIn(marker, second.content)
        # The cached shell renders auth-neutral: both auth control groups
        # are present but hidden and revealed client-side from whoami, and
        # the React list's auth flags default to anonymous until hydration.
        self.assertContains(second, "data-nav-auth-only")
        self.assertContains(second, "data-nav-anon-only")
        self.assertContains(second, 'data-authenticated="false"')
        self.assertContains(second, 'data-can-suggest-company="false"')
        # No per-user surface (rate-limit keys job_search_rl:*) is ever
        # written into the public cache by these page requests.
        for key in cache._cache:
            self.assertNotIn("job_search_rl", str(key))

    def test_algo_page_cache_uses_invalidatable_key_and_score_events_clear_it(self):
        """The algo shell caches under algorithm_<id>_page — a key listed in
        scores.affected_cache_keys — instead of cache_page's request-derived
        key, so score publication clears the rendered page together with the
        result keys (review finding: uninvalidated full-page cache)."""
        self.setup_scores()
        from crank.services import publication

        algo_url = f"/algo/{DEFAULT_ALGORITHM_ID}/"
        page_key = f"algorithm_{DEFAULT_ALGORITHM_ID}_page"
        first = self.client.get(algo_url)
        self.assertEqual(first.status_code, 200)
        self.assertIsNotNone(cache.get(page_key))

        publication.record_event(
            target_type="score",
            target_id=self.organization1.id,
            event_kind="changed",
            payload={"score_type_id": 1, "outcome": "changed"},
        )
        publication.sweep_pending()
        self.assertIsNone(cache.get(page_key))

        # The next request re-primes the cache and is served from it after.
        second = self.client.get(algo_url)
        self.assertEqual(second.status_code, 200)
        self.assertIsNotNone(cache.get(page_key))

    def _queue_flash_message(self, text):
        """Queue a message the same way ``django.contrib.messages`` would,
        without depending on any specific view to produce one (issue #465
        removed the only production call site,
        ``crank.auth.login_required_with_expiry``). Uses the default
        ``CookieStorage`` backend directly to write the same cookie a real
        view's response would carry, then transplants it onto the test
        client so the next request reads it exactly as it would in
        production."""
        from django.contrib.messages import constants
        from django.contrib.messages.storage.cookie import CookieStorage
        from django.http import HttpResponse

        request = RequestFactory().get("/")
        response = HttpResponse()
        storage = CookieStorage(request)
        storage.add(constants.WARNING, text)
        storage.update(response)
        for key, morsel in response.cookies.items():
            self.client.cookies[key] = morsel.value

    def test_cached_algo_shell_excludes_flash_messages(self):
        """The full-page-cached algo shell is shared across every account,
        so per-session flash messages must never render into it: a queued
        message would otherwise be baked into the shared entry and served to
        every later caller (review finding: messages leak through the shared
        page cache)."""
        self.setup_scores()
        algo_url = f"/algo/{DEFAULT_ALGORITHM_ID}/"
        page_key = f"algorithm_{DEFAULT_ALGORITHM_ID}_page"

        # Queue a flash message the way any Django view might (issue #465
        # removed /chat/'s own use of messages.warning; the shared-cache
        # exclusion this test guards applies to any queued message).
        self._queue_flash_message(SESSION_EXPIRED_MESSAGE)

        first = self.client.get(algo_url)
        self.assertEqual(first.status_code, 200)
        self.assertNotContains(first, SESSION_EXPIRED_MESSAGE)
        self.assertNotIn(b"app-messages", first.content)

        # The cached entry itself is message-free, so an independent second
        # account is never poisoned by the first client's queued message.
        self.assertIsNotNone(cache.get(page_key))
        self.assertNotIn(SESSION_EXPIRED_MESSAGE.encode(), cache.get(page_key).content)
        second_client = Client()
        second = second_client.get(algo_url)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.content, first.content)
        self.assertNotContains(second, SESSION_EXPIRED_MESSAGE)

        # Excluding the message from the cached shell never drops it: it
        # stays queued and renders on the next uncached page.
        uncached = self.client.get(self.index_url)
        self.assertEqual(uncached.status_code, 200)
        self.assertContains(uncached, SESSION_EXPIRED_MESSAGE)

    def test_invalid_algo_ids_are_never_full_page_cached(self):
        """A bad algorithm id falls back to the default algorithm, but that
        response must never enter the page cache under the *requested* id:
        no score publication clears algorithm_<bad_id>_page, so it would
        serve stale default-algorithm content until TTL (review finding:
        invalid-ID fallback cached under an uninvalidatable key)."""
        self.setup_scores()
        from crank.services import publication

        bad_url = "/algo/99999/"
        bad_page_key = "algorithm_99999_page"
        first = self.client.get(bad_url)
        self.assertEqual(first.status_code, 200)
        # The fallback still renders the default algorithm's content, but
        # never under a key no score event touches.
        self.assertContains(first, "Test Algorithm")
        self.assertIsNone(cache.get(bad_page_key))

        # Repeated fallback requests keep working and keep staying uncached.
        second = self.client.get(bad_url)
        self.assertEqual(second.status_code, 200)
        self.assertIsNone(cache.get(bad_page_key))

        # A changed score for the default algorithm is visible on the next
        # fallback render: nothing stale survives a publication sweep.
        Score.objects.filter(target_id=self.organization2.id).update(score=9.0)
        publication.record_event(
            target_type="score",
            target_id=self.organization2.id,
            event_kind="changed",
            payload={"score_type_id": 1, "outcome": "changed"},
        )
        publication.sweep_pending()
        fresh = self.client.get(bad_url)
        self.assertEqual(fresh.status_code, 200)
        fresh_content = fresh.content.decode()
        self.assertLess(
            fresh_content.index("Org 2"),
            fresh_content.index("Org 1"),
            "Org 2 outranks Org 1 after the changed score, so a stale "
            "cached fallback would still show Org 1 first",
        )

        # Valid ids keep using the explicitly invalidatable cache key.
        valid = self.client.get(f"/algo/{DEFAULT_ALGORITHM_ID}/")
        self.assertEqual(valid.status_code, 200)
        self.assertIsNotNone(
            cache.get(f"algorithm_{DEFAULT_ALGORITHM_ID}_page")
        )

    def test_template_else_branch_shows_message_when_no_algorithm(self):
        ScoreAlgorithm.objects.all().delete()
        request = self.factory.get(self.index_url)
        self.add_session_to_request(request)
        request.session['algorithm_id'] = '1'
        response = self.view(request)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response,
                            "No organizations are available or the Score Algorithm you specified doesn't exist.")
        self.assertNotContains(response, 'organization-list-config')

    def test_template_submitform_preserves_query_params(self):
        self.setup_scores()
        response = self.client.get(self.index_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'window.location.search')
        self.assertContains(response, 'algorithmUrl + window.location.search')

    # --- superseded-score exclusion (issue #461) ---

    def setup_superseded_scores(self):
        """Org 1 has a superseded 1.0 row plus its active 5.0 replacement, an
        active score on a deactivated type, and a deactivated weight row that
        would duplicate the join if not filtered. Org 2 has only a superseded
        row."""
        self.organization1 = Organization.objects.create(
            name="Org 1", funding_round="P", rto_policy="R", accelerated_vesting=True,
            url='org1.com', gives_ratings=True, public=True, type="C",
        )
        self.organization2 = Organization.objects.create(
            name="Org 2", funding_round="B", rto_policy="H", accelerated_vesting=False,
            url='org2.com', gives_ratings=False, public=False, type="C",
        )
        self.type_a = ScoreType.objects.create(name='Weighted Type')
        self.type_b = ScoreType.objects.create(name='Light Type')
        self.retired_type = ScoreType.objects.create(name='Retired Type', status=0)
        ScoreAlgorithmWeight.objects.create(
            algorithm_id=DEFAULT_ALGORITHM_ID, type_id=self.type_a.id, weight=3.0)
        ScoreAlgorithmWeight.objects.create(
            algorithm_id=DEFAULT_ALGORITHM_ID, type_id=self.type_b.id, weight=1.0)
        # A deactivated weight row for the same type/algorithm must not
        # duplicate the join and skew the weighted SUM.
        ScoreAlgorithmWeight.objects.create(
            algorithm_id=DEFAULT_ALGORITHM_ID, type_id=self.type_a.id, weight=3.0, status=0)
        # Superseded 1.0 row replaced by an active 5.0 for the weighted type.
        Score.objects.create(source_id=self.organization2.id, target_id=self.organization1.id,
                             score=1.0, type_id=self.type_a.id, status=0)
        Score.objects.create(source_id=self.organization2.id, target_id=self.organization1.id,
                             score=5.0, type_id=self.type_a.id)
        # Active score for the second active type.
        Score.objects.create(source_id=self.organization2.id, target_id=self.organization1.id,
                             score=1.0, type_id=self.type_b.id)
        # A stray active score on a deactivated type must not count.
        Score.objects.create(source_id=self.organization2.id, target_id=self.organization1.id,
                             score=4.0, type_id=self.retired_type.id)
        # Org 2 only has a superseded row: it must be excluded entirely.
        Score.objects.create(source_id=self.organization1.id, target_id=self.organization2.id,
                             score=2.0, type_id=self.type_a.id, status=0)

    def test_superseded_scores_excluded_from_rankings_and_completeness(self):
        self.setup_superseded_scores()
        response = self.client.get(self.index_url + 'algo/1/')
        self.assertEqual(response.status_code, 200)
        rows = {row['id']: row for row in response.context_data['top_organization_list']}
        # The superseded-only org has no active scores, so it never appears.
        self.assertNotIn(self.organization2.id, rows)
        org1 = rows[self.organization1.id]
        # Only active rows count: (5.0*3 + 1.0*1) / (3 + 1) = 4.0. Averaging
        # the historical 1.0 row, the deactivated-type 4.0, or double-counting
        # the deactivated weight row would all shift this value.
        self.assertEqual(org1['avg_score'], 4.0)
        self.assertEqual(org1['ranking'], 1)
        # Both active types have active scores; the retired type's score must
        # not inflate completeness above 100.
        self.assertEqual(org1['profile_completeness'], 100.0)

    def test_profile_completeness_counts_unscored_active_type_as_missing(self):
        org = Organization.objects.create(
            name="Half Org", funding_round="P", rto_policy="R", url='half.com',
            gives_ratings=True, public=True, type="C",
        )
        scorer = Organization.objects.create(
            name="Scorer Org", gives_ratings=True, url='scorer.com', type="C",
        )
        scored_type = ScoreType.objects.create(name='Scored Type')
        unscored_type = ScoreType.objects.create(name='Unscored Type')
        ScoreAlgorithmWeight.objects.create(
            algorithm_id=DEFAULT_ALGORITHM_ID, type_id=scored_type.id, weight=1.0)
        ScoreAlgorithmWeight.objects.create(
            algorithm_id=DEFAULT_ALGORITHM_ID, type_id=unscored_type.id, weight=1.0)
        Score.objects.create(source_id=scorer.id, target_id=org.id,
                             score=5.0, type_id=scored_type.id)

        response = self.client.get(self.index_url + 'algo/1/')
        self.assertEqual(response.status_code, 200)
        rows = {row['id']: row for row in response.context_data['top_organization_list']}
        # An active type with no active score is a missing dimension: 1 of 2.
        self.assertEqual(rows[org.id]['profile_completeness'], 50.0)
        self.assertEqual(rows[org.id]['avg_score'], 5.0)

    def test_index_ignores_pre_fix_algorithm_results_cache_entry(self):
        """Post-deploy rollout regression (issue #461 review, finding 1).

        A deploy swaps the code while the shared cache keeps whatever the
        previous build wrote. Seed the unversioned ``algorithm_<id>_results``
        key with a pre-#461 ranking (the superseded-only org ranked, the
        weighted average dragged down by historical rows) and prove the page
        recomputes from the active-score predicates instead.
        """
        self.setup_superseded_scores()
        pre_fix_rows = [
            {'id': self.organization2.id, 'name': 'Org 2', 'avg_score': 2.0, 'ranking': 1},
            {'id': self.organization1.id, 'name': 'Org 1', 'avg_score': 1.0, 'ranking': 2},
        ]
        cache.set(f'algorithm_{DEFAULT_ALGORITHM_ID}_results', pre_fix_rows)

        response = self.client.get(self.index_url)

        self.assertEqual(response.status_code, 200)
        rows = {row['id']: row for row in response.context_data['top_organization_list']}
        # Freshly computed rankings: the superseded-only org never appears,
        # and Org 1's average reflects only its active rows ((5.0*3 + 1.0*1)
        # / (3 + 1) == 4.0), not the poisoned 1.0 from the legacy entry.
        self.assertNotIn(self.organization2.id, rows)
        self.assertEqual(rows[self.organization1.id]['avg_score'], 4.0)
        # The recompute is cached under the versioned key; the pre-fix entry
        # is abandoned in place under the legacy key and never read again.
        self.assertIsNotNone(
            cache.get(algorithm_results_cache_key(DEFAULT_ALGORITHM_ID))
        )
        self.assertEqual(
            cache.get(f'algorithm_{DEFAULT_ALGORITHM_ID}_results'), pre_fix_rows
        )

    def test_algo_page_cache_key_carries_the_score_cache_version(self):
        """The /algo/<id/> page is cached under algorithm_{id}_page."""
        self.setup_superseded_scores()
        response = self.client.get(
            self.index_url + f'algo/{DEFAULT_ALGORITHM_ID}/'
        )
        self.assertEqual(response.status_code, 200)
        # The algo_page view caches under the explicit ``algorithm_{id}_page``
        # key (not the default cache_page prefix), so score publication can
        # invalidate it together with the result keys.
        expected_key = f'algorithm_{DEFAULT_ALGORITHM_ID}_page'
        cache_keys = list(cache._cache.keys())
        self.assertTrue(
            any(expected_key in key for key in cache_keys),
            f'no page-cache entry under {expected_key!r}: {cache_keys}',
        )

    def test_company_sign_in_url_template_is_server_validated(self):
        """The company dialog's sign-in CTA is built server-side (issue #465
        AC-7): the client only substitutes the organization id it already
        holds, so the ``next`` target is vetted by ``safe_next_url`` exactly
        like every other handoff."""
        response = self.client.get(self.index_url)

        template = response.context['company_sign_in_url_template']
        self.assertIn(COMPANY_ID_PLACEHOLDER, template)
        self.assertTrue(template.startswith('/accounts/login/?next='))
        # The placeholder survives urlencode byte-identical, so the client
        # can find it; the path and separators are encoded.
        self.assertIn('next=%2F%3Fcompany%3D' + COMPANY_ID_PLACEHOLDER, template)
        self.assertContains(
            response, f'data-sign-in-url-template="{escape(template)}"'
        )

    def test_company_sign_in_url_template_returns_to_the_algo_shell_it_came_from(self):
        """A visitor exploring /algo/<id>/ returns to that page, not to /."""
        self.setup_scores()
        algo_url = f'/algo/{DEFAULT_ALGORITHM_ID}/'

        response = self.client.get(algo_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'next=%2Falgo%2F{DEFAULT_ALGORITHM_ID}%2F%3Fcompany%3D{COMPANY_ID_PLACEHOLDER}',
        )

    def test_company_sign_in_url_template_carries_no_account_identity(self):
        """It is embedded in the shared cached shell, so it must be the same
        for every account."""
        user = User.objects.create_user('rankings-user', password='password')
        anonymous = self.client.get(self.index_url).context['company_sign_in_url_template']

        self.client.force_login(user)
        authenticated = self.client.get(self.index_url).context['company_sign_in_url_template']

        self.assertEqual(anonymous, authenticated)
        self.assertNotIn(user.username, authenticated)
