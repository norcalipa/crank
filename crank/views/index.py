import os

import markdown
from django.core.cache import cache
from django.views import generic

from crank.models.organization import Organization
from crank.models.score import ScoreAlgorithm
from crank.settings import BASE_DIR

CONTENT_DIR = os.path.join(str(BASE_DIR), "crank/content")

class IndexView(generic.ListView):
    def __init__(self):
        super().__init__()
        self.object_list = None
        self.kwargs = {}

    template_name = "crank/index.html"
    context_object_name = "top_organization_list"

    def get_queryset(self):
        algorithm_id = None
        # first, check for a valid algorithm_id in the URL
        try:
            if 'algorithm_id' in self.kwargs:
                algorithm_id = self.kwargs['algorithm_id']
                if algorithm_id != self.request.session.get("algorithm_id"):
                    self.request.session["algorithm_id"] = algorithm_id
        except ValueError:
            pass

        # if no algorithm_id in the URL, check for one in the session
        if not algorithm_id:
            algorithm_id = self.request.session.get("algorithm_id")

        # if no algorithm_id in the session, use the default
        if not algorithm_id:
            algorithm_id = 1
            self.request.session["algorithm_id"] = algorithm_id

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
            AVG(cs.score) 
            AS avg_type_score, 
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
     GROUP BY co.id, co.name, ct.name) orgs,
    /* Subquery that gives the number of score types present for an org */
    (SELECT target_id, count(*) AS score_type_count FROM
        (SELECT target_id,
                type_id,
                COUNT(type_id)
         FROM crank_score cs
         WHERE status = 1
         GROUP BY target_id, type_id)
     GROUP BY target_id) score_types 
WHERE score_types.target_id = orgs.id 
GROUP BY id, name, type
ORDER BY avg_score DESC''', [algorithm_id])
            return self.object_list
        except Organization.DoesNotExist:
            self.object_list = []

        return self.object_list

    def get_context_data(self, **kwargs):
        if kwargs is None:
            kwargs = {}
        context = super().get_context_data(**kwargs)
        context['algorithm'] = self.get_algorithm_details()
        return context

    def get_algorithm_details(self):
        algorithm_id = self.request.session.get("algorithm_id")

        try:
            algorithm = ScoreAlgorithm.objects.get(id=algorithm_id)
            file_path = os.path.join(CONTENT_DIR, algorithm.description_content)
            with open(file_path, 'r') as file:
                md_content = file.read()
            html_content = markdown.markdown(md_content)
            algorithm.html_description_content = html_content
            return algorithm
        except ScoreAlgorithm.DoesNotExist:
            return None