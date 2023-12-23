import os

import markdown
from django.shortcuts import redirect
from django.urls import reverse
from django.views import generic

from crank.models.organization import Organization
from crank.models.score import ScoreAlgorithm
from crank.settings.base import BASE_DIR

CONTENT_DIR = os.path.join(str(BASE_DIR), "crank/content")


class IndexView(generic.ListView):
    algorithm_cache = {}

    def __init__(self):
        super().__init__()
        self.object_list = None
        self.kwargs = {}
        self.algorithm_id = None
        self.algorithm = None
        self.error = None

    template_name = "crank/index.html"
    context_object_name = "top_organization_list"

    def _check_algorithm_id(self):
        if not self.algorithm:
            if 'algorithm_id' in self.kwargs:
                self.algorithm_id = self.kwargs['algorithm_id']
            if self.algorithm_id in IndexView.algorithm_cache.keys():
                self.algorithm = IndexView.algorithm_cache[self.algorithm_id]
                return
            if not ScoreAlgorithm.objects.filter(id=self.algorithm_id).exists():
                self.algorithm_id = 1
                self.request.session["algorithm_id"] = self.algorithm_id
            try:
                if not self.algorithm:
                    self.algorithm = ScoreAlgorithm.objects.get(id=self.algorithm_id)
                    self.algorithm_cache[self.algorithm_id] = self.algorithm
            except ScoreAlgorithm.DoesNotExist:
                pass # we will handle empty algorithms by returning an empty object list

    def get_queryset(self):
        # if no algorithm_id in the URL, check for one in the session
        if not self.algorithm_id:
            self.algorithm_id = self.request.session.get("algorithm_id")

        # if no algorithm_id in the session, use the default
        try:
            self._check_algorithm_id()
            if not self.algorithm:
                # this should generally not happen since there should *always* be a default algorithm
                self.object_list = []
                return self.object_list
        except ValueError:
            return redirect('/')

        """Return all active organizations with scores in descending order."""
        try:
            self.object_list = Organization.objects.raw('''
SELECT orgs.id,
       orgs.name,
       orgs.type,
       orgs.rto_policy,
       orgs.funding_round,
       SUM(orgs.avg_type_score * orgs.weight) / SUM(orgs.weight) AS avg_score,
       (CAST(score_types.score_type_count AS REAL) /
       /* Admittedly long calculation dividing total score types by the number present for each org */
       (SELECT COUNT(*) FROM crank_scoretype ct WHERE ct.status = 1) * 100) AS profile_completeness
FROM
    /* Subquery that gives core org details and avg score by type */
    (SELECT co.id,
            co.name,
            co.type,
            co.rto_policy,
            co.funding_round,
            AVG(cs.score) AS avg_type_score,
            cw.weight,
            ct.name AS score_type
     FROM crank_organization AS co,
          crank_score AS cs,
          crank_scoretype AS ct,
          crank_scorealgorithmweight AS cw
     WHERE co.status = 1 AND
     co.id = cs.target_id AND
     cs.type_id = cw.type_id AND
     cw.algorithm_id = %s AND
     cs.type_id = ct.id
     GROUP BY co.id, co.name, co.type, co.rto_policy, co.funding_round, cw.weight, ct.name) orgs,
    /* Subquery that gives the number of score types present for an org */
    (SELECT target_id, count(*) AS score_type_count FROM
        (SELECT target_id,
                type_id,
                COUNT(type_id)
         FROM crank_score cs
         WHERE status = 1
         GROUP BY target_id, type_id) score_counts
     GROUP BY score_counts.target_id) score_types
WHERE score_types.target_id = orgs.id
GROUP BY id, name, type
ORDER BY avg_score DESC''', [self.algorithm_id])
            return self.object_list
        except Organization.DoesNotExist:
            self.object_list = []

        return self.object_list

    def get_context_data(self, **kwargs):
        if kwargs is None:
            kwargs = {}
        context = super().get_context_data(**kwargs)
        context['algorithm'] = self.get_algorithm_details()
        context['all_algorithms'] = ScoreAlgorithm.objects.filter(status=1)
        return context

    def get_algorithm_details(self):
        if self.error:
            return None
        self.algorithm_id = self.request.session.get("algorithm_id")

        try:
            self._check_algorithm_id()
            if not hasattr(self.algorithm, 'html_description_content'):
                file_path = os.path.join(CONTENT_DIR, self.algorithm.description_content)
                with open(file_path, 'r') as file:
                    md_content = file.read()
                html_content = markdown.markdown(md_content)
                self.algorithm.html_description_content = html_content
            return self.algorithm
        except ValueError:
            return redirect(reverse('index'))
