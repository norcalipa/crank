from django.views import generic

# from crank.models.score import Score
from crank.models.organization import Organization


class IndexView(generic.ListView):
    template_name = "crank/index.html"
    context_object_name = "top_organization_list"

    def get_queryset(self):
        """Return all active organizations with scores in descending order."""
        return Organization.objects.raw(
            "SELECT id, name, score_type, AVG(avg_type_score) as avg_score "
            "FROM (SELECT co.id, co.name, AVG(cs.score) AS avg_type_score, ct.name AS score_type " +
            "FROM crank_organization AS co, crank_score AS cs, crank_scoretype AS ct " +
            "WHERE co.status = 1 AND " +
            "co.id = cs.target_id AND " +
            "cs.type_id = ct.id "
            "GROUP BY co.id, co.name, ct.name)" +
            "GROUP BY id, name "
            "ORDER BY avg_score DESC"
        )


class OrganizationView(generic.DetailView):
    model = Organization
    template_name = "crank/organization.html"

    def get_queryset(self):
        """make sure we can't see inactive organizations."""
        return Organization.objects.filter(status=1)