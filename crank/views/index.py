from django.views import generic

from crank.models.organization import Organization


class IndexView(generic.ListView):
    template_name = "crank/index.html"
    context_object_name = "top_organization_list"

    def get_queryset(self):
        """Return all active organizations with scores in descending order."""
        try:
            result = Organization.objects.raw("""
SELECT orgs.id, 
       orgs.name, 
       orgs.type,
       orgs.rto_policy,
       orgs.funding_round,
       AVG(orgs.avg_type_score) AS avg_score,
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
            ct.name AS score_type
     FROM crank_organization AS co, crank_score AS cs, crank_scoretype AS ct
     WHERE co.status = 1 AND
     co.id = cs.target_id AND
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
ORDER BY avg_score DESC""")
            return result
        except Organization.DoesNotExist:
            return []
