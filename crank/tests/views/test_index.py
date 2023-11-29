from django.test import TestCase
from django.test import RequestFactory
from crank.models.organization import Organization

from crank.views.index import IndexView


class IndexViewTests(TestCase):
    def test_index_view(self):
        org_name = "Test Organization"
        org = Organization.objects.create(name=org_name)

        #response = self.client.get('/') # + str(org.id))
        request = RequestFactory().get('/')
        view = IndexView()
        view.request = request
        qs = view.get_queryset()
        self.assertContains(Organization.objects.all())

        #self.assertEqual(response.status_code, 200)
        #self.assertTemplateUsed(response, 'index.html')
