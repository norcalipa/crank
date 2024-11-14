# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import os

import markdown
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import generic
from django.core.cache import cache
from django.conf import settings


from crank.models.organization import Organization
from crank.models.score import ScoreAlgorithm
from crank.settings.base import CONTENT_DIR, DEFAULT_ALGORITHM_ID
from crank.forms.organization_filter import OrganizationFilterForm


class IndexView(generic.ListView):
    algorithm_cache = {}
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

    def _check_algorithm_id(self):
        if not self.algorithm:
            if 'algorithm_id' in self.kwargs:
                self.algorithm_id = self.kwargs['algorithm_id']
            if self.algorithm_id in IndexView.algorithm_cache.keys():
                self.algorithm = IndexView.algorithm_cache[self.algorithm_id]
                return
            if not ScoreAlgorithm.objects.filter(id=self.algorithm_id).exists():
                self.algorithm_id = DEFAULT_ALGORITHM_ID
                self.request.session["algorithm_id"] = self.algorithm_id
            try:
                if not self.algorithm:
                    self.algorithm = ScoreAlgorithm.objects.get(id=self.algorithm_id)
                    self.algorithm_cache[self.algorithm_id] = self.algorithm
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
            self.algorithm_id = self.request.session.get("algorithm_id")

        self.accelerated_vesting = self.request.session.get('accelerated_vesting', False)

        # if no algorithm_id in the session, use the default
        self._check_algorithm_id()
        if not self.algorithm:
            # this should generally not happen since there should *always* be a default algorithm
            self.object_list = []
            return self.object_list

        def fetch_results():
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
                    WHERE co.status = 1 AND cw.algorithm_id = %s
                    GROUP BY co.id, co.name, co.type, co.rto_policy, co.funding_round, co.accelerated_vesting, cw.weight, ct.name
                ) orgs
                JOIN (
                    SELECT target_id, count(*) AS score_type_count
                    FROM (
                        SELECT target_id, type_id, COUNT(type_id)
                        FROM crank_score
                        WHERE status = 1
                        GROUP BY target_id, type_id
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

        cache_key = f'algorithm_{self.algorithm_id}_results'
        self.object_list = cache.get_or_set(cache_key, fetch_results, timeout=settings.CACHE_MIDDLEWARE_SECONDS)
        return self.object_list


    def get_context_data(self, **kwargs):
        if kwargs is None:
            kwargs = {}
        context = super().get_context_data(**kwargs)
        context['algorithm'] = self.get_algorithm_details()
        context['all_algorithms'] = ScoreAlgorithm.objects.filter(status=1)
        context['form'] = OrganizationFilterForm(
            initial={'accelerated_vesting': self.request.session.get('accelerated_vesting')}, request=self.request)

        # Serialize the organization data using JsonResponse
        context['top_organization_list'] = list(self.object_list)

        return context

    def get_algorithm_details(self):
        if self.error:
            return None
        self.algorithm_id = self.request.session.get("algorithm_id")

        self._check_algorithm_id()
        if not hasattr(self.algorithm, 'html_description_content'):
            file_path = os.path.join(CONTENT_DIR, self.algorithm.description_content)
            with open(file_path, 'r') as file:
                md_content = file.read()
            html_content = markdown.markdown(md_content)
            self.algorithm.html_description_content = html_content
        return self.algorithm