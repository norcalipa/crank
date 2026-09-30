# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Staff review of company evidence claims: admin, audit, scope, and the crawl-to-API path."""

import json

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from crank.models.company_profile import CompanyFieldEvidence, CompanyProfileObservation
from crank.models.employer import EmployerAlias
from crank.models.job import JobSourceCatalog
from crank.models.monitoring import OperationalChangeAudit
from crank.models.organization import Organization
from crank.services.company_crawler import crawl_company_profile
from crank.tests.services.test_company_crawler import FakeClient, profile

FieldKey = CompanyFieldEvidence.FieldKey
State = CompanyFieldEvidence.State
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(FIRECRAWL_MAX_PAGES=3, FIRECRAWL_CREDIT_BUDGET=3, CACHES=LOCMEM)
class ClaimReviewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = User.objects.create_user("reviewer", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        self.organization = Organization.objects.create(
            name="Example Labs", url="https://example.test"
        )
        self.source = JobSourceCatalog.objects.create(
            name="Example careers",
            adapter_key="firecrawl-careers",
            base_url="https://jobs.example.test/careers",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        crawl_company_profile(self.source, client=FakeClient([profile()]))
        self.rto = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.RTO_POLICY
        )
        self.url = reverse("admin:crank_companyfieldevidence_changelist")

    def _post_action(self, action, pks, confirm=True):
        data = {"action": action, ACTION_CHECKBOX_NAME: pks, "index": 0}
        if confirm:
            data["confirm"] = "yes"
        return self.client.post(self.url, data)

    def _provenance(self):
        cache.clear()
        response = self.client.get(f"/api/organizations/{self.organization.pk}/provenance/")
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content.decode("utf-8"))

    def test_changelist_defaults_to_open_claims(self):
        accepted = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.COMPANY_NAME
        )
        response = self.client.get(self.url)
        listed = {row.pk for row in response.context["cl"].result_list}
        self.assertIn(self.rto.pk, listed)
        self.assertNotIn(accepted.pk, listed)
        everything = self.client.get(self.url, {"queue": "all"})
        self.assertIn(accepted.pk, {r.pk for r in everything.context["cl"].result_list})

    def test_unconfirmed_accept_renders_confirmation_and_changes_nothing(self):
        response = self._post_action("accept_claims", [self.rto.pk], confirm=False)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/confirm_action.html")
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.PENDING)
        self.assertEqual(OperationalChangeAudit.objects.count(), 0)

    def test_confirmed_accept_verifies_claim_and_audits(self):
        self.assertNotIn(
            FieldKey.RTO_POLICY, [f["field_key"] for f in self._provenance()["fields"]]
        )
        self._post_action("accept_claims", [self.rto.pk])

        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.ACCEPTED)
        self.assertIsNotNone(self.rto.last_verified_at)
        audit = OperationalChangeAudit.objects.get(target_type="company_field_evidence")
        self.assertEqual(audit.action, "claim_accepted")
        self.assertEqual(audit.actor, self.staff)
        self.assertTrue(audit.confirmed)
        entry = next(f for f in self._provenance()["fields"] if f["field_key"] == "rto_policy")
        self.assertEqual(entry["state"], "accepted")
        self.assertEqual(entry["value"], "Remote first")

    def test_confirmed_reject_keeps_row_but_never_resolves(self):
        self._post_action("reject_claims", [self.rto.pk])

        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.REJECTED)
        self.assertEqual(
            OperationalChangeAudit.objects.get(target_type="company_field_evidence").action,
            "claim_rejected",
        )
        self.assertNotIn(
            "rto_policy", [f["field_key"] for f in self._provenance()["fields"]]
        )

    def test_non_open_rows_are_skipped_without_audit(self):
        accepted = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.COMPANY_NAME
        )
        response = self._post_action("accept_claims", [accepted.pk, self.rto.pk], confirm=True)
        self.assertEqual(response.status_code, 302)
        accepted.refresh_from_db()
        self.assertEqual(accepted.state, State.ACCEPTED)
        self.assertEqual(
            OperationalChangeAudit.objects.filter(target_type="company_field_evidence").count(), 1
        )

    def test_direct_action_call_without_confirmation_is_a_noop(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from crank.admin import CompanyFieldEvidenceAdmin

        request = RequestFactory().post(self.url)
        request.user = self.staff
        model_admin = CompanyFieldEvidenceAdmin(CompanyFieldEvidence, AdminSite())
        model_admin.message_user = lambda *a, **k: None
        model_admin.accept_claims(request, CompanyFieldEvidence.objects.filter(pk=self.rto.pk))
        model_admin.reject_claims(request, CompanyFieldEvidence.objects.filter(pk=self.rto.pk))
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.PENDING)

    def test_direct_confirmed_call_skips_rows_that_are_no_longer_open(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from crank.admin import CompanyFieldEvidenceAdmin

        accepted = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.COMPANY_NAME
        )
        request = RequestFactory().post(self.url, {"confirm": "yes"})
        request.user = self.staff
        model_admin = CompanyFieldEvidenceAdmin(CompanyFieldEvidence, AdminSite())
        messages = []
        model_admin.message_user = lambda _r, text, **_k: messages.append(text)
        model_admin.reject_claims(
            request, CompanyFieldEvidence.objects.filter(pk__in=[accepted.pk, self.rto.pk])
        )
        accepted.refresh_from_db()
        self.rto.refresh_from_db()
        self.assertEqual(accepted.state, State.ACCEPTED)
        self.assertEqual(self.rto.state, State.REJECTED)
        self.assertEqual(messages, ["1 claim(s) rejected; 1 skipped (not open)."])

    def test_direct_observation_review_without_confirmation_is_a_noop(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from crank.admin import CompanyProfileObservationAdmin

        observation = CompanyProfileObservation.objects.get(organization=self.organization)
        request = RequestFactory().post("/admin/")
        request.user = self.staff
        model_admin = CompanyProfileObservationAdmin(CompanyProfileObservation, AdminSite())
        model_admin.message_user = lambda *a, **k: None
        model_admin.reject_observations(
            request, CompanyProfileObservation.objects.filter(pk=observation.pk)
        )
        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.AUTO_APPLIED)
        self.assertFalse(OperationalChangeAudit.objects.exists())

    def test_non_staff_cannot_reach_evidence_admin(self):
        plain = User.objects.create_user("plain", password="pw")
        self.client.force_login(plain)
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_no_add_or_delete_permission(self):
        self.assertEqual(self.client.get(reverse("admin:crank_companyfieldevidence_add")).status_code, 403)
        delete = reverse("admin:crank_companyfieldevidence_delete", args=[self.rto.pk])
        self.assertEqual(self.client.get(delete).status_code, 403)

    def _change(self, claim, scope):
        url = reverse("admin:crank_companyfieldevidence_change", args=[claim.pk])
        return self.client.post(url, {"scope_json": json.dumps(scope)})

    def test_scope_edit_is_cleaned_and_audited(self):
        response = self._change(
            self.rto,
            {"countries": ["US", "DE", "US"], "role_families": ["Engineering"], "claimed_domain": "example.test"},
        )
        self.assertEqual(response.status_code, 302)
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.scope_json["countries"], ["DE", "US"])
        self.assertEqual(
            OperationalChangeAudit.objects.get(action="scope_change").target_id, str(self.rto.pk)
        )

    def test_scope_rejects_teams_unknown_keys_and_bad_shapes(self):
        for scope in (
            {"teams": ["Payments"]},
            {"team": "Payments"},
            {"colour": "blue"},
            {"countries": "US"},
            {"countries": [""]},
            {"countries": ["x"] * 11},
            {"countries": ["\u200b"]},
            ["not", "an", "object"],
        ):
            response = self._change(self.rto, scope)
            self.assertEqual(response.status_code, 200, scope)
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.scope_json, {"claimed_domain": "example.test"})
        self.assertFalse(OperationalChangeAudit.objects.filter(action="scope_change").exists())

    def test_scope_is_read_only_once_accepted(self):
        accepted = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.COMPANY_NAME
        )
        url = reverse("admin:crank_companyfieldevidence_change", args=[accepted.pk])
        self.client.post(url, {"scope_json": json.dumps({"countries": ["US"]})})
        accepted.refresh_from_db()
        self.assertEqual(accepted.scope_json, {"claimed_domain": "example.test"})

    def test_unchanged_scope_saves_without_audit(self):
        self._change(self.rto, {"claimed_domain": "example.test"})
        self.assertFalse(OperationalChangeAudit.objects.filter(action="scope_change").exists())

    def test_observation_review_requires_confirmation_and_audits(self):
        observation = CompanyProfileObservation.objects.filter(organization=self.organization).get()
        url = reverse("admin:crank_companyprofileobservation_changelist")
        data = {"action": "reject_observations", ACTION_CHECKBOX_NAME: [observation.pk], "index": 0}
        self.assertTemplateUsed(self.client.post(url, data), "admin/confirm_action.html")
        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.AUTO_APPLIED)
        self.client.post(url, {**data, "confirm": "yes"})
        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.REJECTED)
        audit = OperationalChangeAudit.objects.get(target_type="company_profile_observation")
        self.assertEqual(audit.action, "review_rejected")


@override_settings(FIRECRAWL_MAX_PAGES=3, FIRECRAWL_CREDIT_BUDGET=3, CACHES=LOCMEM)
class AmbiguousIdentityTests(TestCase):
    def test_ambiguous_identity_creates_no_claims_or_evidence(self):
        Organization.objects.create(name="Example Labs", url="https://example.test")
        second = Organization.objects.create(name="Example Labs Two", url="https://other.test")
        EmployerAlias.objects.create(
            organization=second,
            kind=EmployerAlias.AliasKind.DOMAIN,
            value="example.test",
            status=EmployerAlias.Status.APPROVED,
        )
        source = JobSourceCatalog.objects.create(
            name="Example careers",
            adapter_key="firecrawl-careers",
            base_url="https://jobs.example.test/careers",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )

        result = crawl_company_profile(source, client=FakeClient([profile()]))

        self.assertEqual(result.conflicted, 1)
        self.assertEqual(CompanyFieldEvidence.objects.count(), 0)
