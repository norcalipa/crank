from django.test import TestCase, RequestFactory
from django.contrib.admin.sites import AdminSite
from crank.admin import ScoreInline, ScoreAlgorithmWeightInline
from crank.models.organization import Organization
from crank.models.score import Score, ScoreAlgorithm, ScoreType
from django.contrib.auth.models import User


class MockRequest:
    def __init__(self, user=None):
        self.user = user
        self.session = {}


class ScoreInlineTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.inline = ScoreInline(Organization, self.site)
        self.user = User.objects.create_user(username='testuser', password='12345')
        self.request = MockRequest(user=self.user)

    def test_get_formset_disables_inline_icons(self):
        formset = self.inline.get_formset(self.request)
        self.assertFalse(formset.form.base_fields['type'].widget.can_view_related)
        self.assertFalse(formset.form.base_fields['type'].widget.can_add_related)
        self.assertFalse(formset.form.base_fields['type'].widget.can_change_related)
        self.assertFalse(formset.form.base_fields['type'].widget.can_delete_related)
        self.assertFalse(formset.form.base_fields['source'].widget.can_view_related)
        self.assertFalse(formset.form.base_fields['source'].widget.can_add_related)
        self.assertFalse(formset.form.base_fields['source'].widget.can_change_related)
        self.assertFalse(formset.form.base_fields['source'].widget.can_delete_related)


class ScoreAlgorithmWeightInlineTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.inline = ScoreAlgorithmWeightInline(ScoreAlgorithm, self.site)
        self.user = User.objects.create_user(username='testuser', password='12345')
        self.request = MockRequest(user=self.user)

    def test_get_formset_disables_inline_icons(self):
        formset = self.inline.get_formset(self.request)
        self.assertFalse(formset.form.base_fields['type'].widget.can_view_related)
        self.assertFalse(formset.form.base_fields['type'].widget.can_add_related)
        self.assertFalse(formset.form.base_fields['type'].widget.can_change_related)
        self.assertFalse(formset.form.base_fields['type'].widget.can_delete_related)
