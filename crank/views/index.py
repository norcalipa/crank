# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import os

import markdown
from django.db import connection
from django.views import generic
from django.core.cache import cache
from django.conf import settings
from crank.auth import sign_in_url
from crank.models.score import ScoreAlgorithm
from crank.services.scores import algorithm_results_cache_key
from crank.settings.base import CONTENT_DIR, DEFAULT_ALGORITHM_ID
from crank.forms.organization_filter import OrganizationFilterForm

#: Stand-in for the organization id inside the company sign-in URL template
#: (issue #465 AC-7). The whole URL — including the ``next`` target and its
#: encoding — is built and validated server-side by
#: :func:`crank.auth.sign_in_url`; the client only substitutes an integer id
#: it already holds, so no client-assembled path ever reaches the redirect
#: guard. Contains only unreserved characters, so ``urlencode`` leaves it
#: byte-identical and the client can find it in the emitted URL.
COMPANY_ID_PLACEHOLDER = "__COMPANY_ID__"


class IndexView(generic.ListView):
    template_name = "crank/index.html"
    context_object_name = "top_organization_list"
    paginate_by = 15

    def __init__(self):
        super().__init__()
        self.object_list = None
        self.kwargs = {}
        self.algorithm_id = None
        self.algorithm = None
        self.error = None
        self.accelerated_vesting = None
        cache_key = 'algorithm_object_list'
        self.algorithms = cache.get(cache_key)
        if not self.algorithms:
            self.algorithms = ScoreAlgorithm.objects.filter(status=1)
            cache.set(cache_key, self.algorithms, timeout=settings.CACHE_MIDDLEWARE_SECONDS)

    def _check_algorithm_id(self):
        if not self.algorithm:
            if 'algorithm_id' in self.kwargs:
                self.algorithm_id = int(self.kwargs['algorithm_id'])

            if self.algorithm_id:
                self.algorithm = self.algorithms.filter(id=int(self.algorithm_id)).first()
                if self.algorithm:
                    return

            self.algorithm_id = DEFAULT_ALGORITHM_ID
            self.request.session["algorithm_id"] = self.algorithm_id
            try:
                if not self.algorithm:
                    self.algorithm = self.algorithms.filter(id=self.algorithm_id).first()
            except ScoreAlgorithm.DoesNotExist:
                pass  # we will handle empty algorithms by returning an empty object list

    def post(self, request, *args, **kwargs):
        self.kwargs = kwargs
        form = OrganizationFilterForm(self.request.POST or None, request=self.request)
        self.accelerated_vesting = form.clean_accelerated_vesting()
        return self.get(request, *args, **kwargs)

    def get_queryset(self):
        # if no algorithm_id in the URL, check for one in the session
        if not self.algorithm_id:
            self.algorithm_id = int(self.request.session.get(
                "algorithm_id")) if "algorithm_id" in self.request.session else DEFAULT_ALGORITHM_ID

        self.accelerated_vesting = self.request.session.get('accelerated_vesting', False)

        # if no algorithm_id in the session, use the default
        self._check_algorithm_id()
        if not self.algorithm:
            # this should generally not happen since there should *always* be a default algorithm
            self.object_list = []
            return self.object_list

        def fetch_results():
            # Active-read predicates mirror the single documented definition in
            # crank.services.scores.active_score_summary_rows: only active score
            # rows (cs.status = 1), of active score types (ct.status = 1),
            # joined through active algorithm weights (cw.status = 1) -- an
            # inactive weight row would otherwise duplicate the join and skew
            # the weighted SUM. Raw SQL cannot call the ORM helper without a
            # rewrite, so keep this predicate set in sync with it.
            #
            # Missing dimensions: profile_completeness counts active types
            # with at least one active score (numerator) over ALL active
            # types (denominator), so an active type with no active score
            # counts as a missing dimension and completeness stays <= 100.
            query = '''
            SELECT id, name, type, rto_policy, funding_round, accelerated_vesting, avg_score, profile_completeness, RANK() OVER (ORDER BY avg_score desc) as ranking
            FROM (
                SELECT orgs.id, orgs.name, orgs.type, orgs.rto_policy, orgs.funding_round, orgs.accelerated_vesting, 
                       SUM(orgs.avg_type_score * orgs.weight) / SUM(orgs.weight) AS avg_score,
                       (CAST(score_types.score_type_count AS REAL) / (SELECT COUNT(*) FROM crank_scoretype ct WHERE ct.status = 1) * 100) AS profile_completeness
                FROM (
                    SELECT co.id, co.name, co.type, co.rto_policy, co.funding_round, co.accelerated_vesting, 
                           AVG(cs.score) AS avg_type_score, cw.weight, ct.name AS score_type
                    FROM crank_organization AS co
                    JOIN crank_score AS cs ON co.id = cs.target_id
                    JOIN crank_scoretype AS ct ON cs.type_id = ct.id
                    JOIN crank_scorealgorithmweight AS cw ON cs.type_id = cw.type_id
                    WHERE co.status = 1 AND cs.status = 1 AND ct.status = 1 AND cw.status = 1 AND cw.algorithm_id = %s
                    GROUP BY co.id, co.name, co.type, co.rto_policy, co.funding_round, co.accelerated_vesting, cw.weight, ct.name
                ) orgs
                JOIN (
                    SELECT target_id, count(*) AS score_type_count
                    FROM (
                        SELECT cs2.target_id, cs2.type_id, COUNT(cs2.type_id)
                        FROM crank_score AS cs2
                        JOIN crank_scoretype AS ct2 ON cs2.type_id = ct2.id
                        WHERE cs2.status = 1 AND ct2.status = 1
                        GROUP BY cs2.target_id, cs2.type_id
                    ) score_counts
                    GROUP BY score_counts.target_id
                ) score_types ON score_types.target_id = orgs.id
                GROUP BY id, name, type, rto_policy, funding_round, accelerated_vesting
            ) scored_results
            '''

            with connection.cursor() as cursor:
                cursor.execute(query, [self.algorithm_id])
                columns = [col[0] for col in cursor.description]
                object_list = [dict(zip(columns, row)) for row in cursor.fetchall()]

            return object_list

        cache_key = algorithm_results_cache_key(self.algorithm_id)
        self.object_list = cache.get_or_set(cache_key, fetch_results, timeout=settings.CACHE_MIDDLEWARE_SECONDS)
        return self.object_list

    def get_context_data(self, **kwargs):
        if kwargs is None:
            kwargs = {}
        context = super().get_context_data(**kwargs)
        context['algorithm'] = self.get_algorithm_details()
        context['all_algorithms'] = self.algorithms.filter(status=1)
        context['form'] = OrganizationFilterForm(
            initial={'accelerated_vesting': self.request.session.get('accelerated_vesting')}, request=self.request)

        context['top_organization_list'] = list(self.object_list)

        # The /algo/<id>/ route is full-page cached and shared across every
        # account (issue #470): its shell renders auth-neutral (nav auth
        # controls present but hidden, auth dataset attributes neutral) and
        # app-nav.js hydrates auth-dependent state per request from the
        # uncached whoami endpoint. Server-rendered pages keep the
        # user-specific branches.
        context['auth_neutral_shell'] = 'algorithm_id' in self.kwargs

        # Sign-in handoff for the company details dialog (issue #465 AC-7):
        # a signed-out visitor who opens a company gets a CTA that returns
        # them to this same page with that company reopened. Built here, not
        # in the client, so the `next` target passes the same
        # `safe_next_url` guard as every other handoff. Carries no account
        # identity, so it is safe in the shared cached shell.
        context['company_sign_in_url_template'] = sign_in_url(
            self.request,
            next_url=f'{self.request.path}?company={COMPANY_ID_PLACEHOLDER}',
        )

        return context

    def get_algorithm_details(self):
        if self.error:
            return None

        self._check_algorithm_id()
        if self.algorithm and not hasattr(self.algorithm, 'html_description_content'):
            def get_html_content():
                file_path = os.path.join(CONTENT_DIR, self.algorithm.description_content)
                with open(file_path, 'r') as file:
                    md_content = file.read()
                return markdown.markdown(md_content)

            self.algorithm.html_description_content = cache.get_or_set(
                f'algorithm_{self.algorithm_id}_description', get_html_content(), timeout=settings.CACHE_MIDDLEWARE_SECONDS)
        return self.algorithm


def _algorithm_id_is_cacheable(algorithm_id):
    """Return whether ``algorithm_id`` resolves to a published algorithm.

    ``IndexView._check_algorithm_id`` falls back to ``DEFAULT_ALGORITHM_ID``
    for any id that does not resolve, so a fallback response renders the
    default algorithm while the URL still names the bad id. Caching that
    response under ``algorithm_<bad_id>_page`` would park default-algorithm
    content under a key no score publication ever clears — ``affected_cache_keys``
    only emits ``algorithm_<resolved_id>_*`` — so such responses must never
    enter the shared page cache (issue #470 review finding).
    """
    return ScoreAlgorithm.objects.filter(id=algorithm_id, status=1).exists()


def algo_page(request, algorithm_id):
    """Explicitly keyed full-page cache for the /algo/<algorithm_id>/ shell.

    ``cache_page`` derives its key from the request (URL, Vary headers,
    cookies), so the publication outbox cannot invalidate the rendered page;
    issue #470 publishes score changes as events whose affected keys must
    clear the full-page HTML together with the algorithm result keys. This
    view caches the rendered shell under ``algorithm_{algorithm_id}_page`` —
    a key listed in ``scores.affected_cache_keys`` — so both the
    ``on_commit`` fast path and the publication sweep clear it. The shell is
    auth-neutral (see ``_navigation.html``), so one entry safely serves every
    account; POST (filter submissions) is never cached. Unresolvable
    algorithm ids are never served from or written to the cache: the view
    falls back to the default algorithm for them, and caching that content
    under the requested id would store default-algorithm content under a key
    score publication never invalidates.
    """
    cache_key = f'algorithm_{algorithm_id}_page'
    cacheable = _algorithm_id_is_cacheable(algorithm_id)
    if request.method == 'GET' and cacheable:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    response = IndexView.as_view()(request, algorithm_id=algorithm_id)
    if request.method == 'GET' and response.status_code == 200 and cacheable:
        # TemplateResponse must be rendered before it can be cached.
        if hasattr(response, 'render') and getattr(response, 'is_rendered', None) is False:
            response.render()
        cache.set(cache_key, response, timeout=settings.CACHE_MIDDLEWARE_SECONDS)
    return response
