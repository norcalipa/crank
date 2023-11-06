from django.views import generic

from .models import Organization, Score


class IndexView(generic.ListView):
    template_name = "crank/index.html"
    context_object_name = "top_organization_list"

    def get_queryset(self):
        """Return all active organizations with scores in descending order."""
        return Organization.objects.raw(
            "SELECT *, AVG(avg_type_score) as avg_score "
            "FROM (SELECT *, AVG(cs.score) AS avg_type_score, ct.name AS score_type " +
            "FROM crank_organization AS co, crank_score AS cs, crank_scoretype AS ct " +
            "WHERE co.status = 1 AND " +
            "co.id = cs.target_id AND " +
            "cs.type_id = ct.id "
            "GROUP BY ct.name)" +
            "ORDER BY avg_score DESC"
        )


class OrganizationView(generic.DetailView):
    model = Organization
    template_name = "crank/organization.html"

    def get_queryset(self):
        """make sure we can't see inactive organizations."""
        return Organization.objects.filter(status=1)

    def get_scores(self):
        return Score.objects.filter(target=self.id)
