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
from crank.services import company_evidence
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

    def test_confirmation_page_describes_the_claim_and_escapes_hostile_text(self):
        CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(
            value_text="<script>alert(1)</script> Remote first"
        )
        response = self._post_action("accept_claims", [self.rto.pk], confirm=False)
        html = response.content.decode("utf-8")
        for expected in ("Example Labs", "rto_policy", "currently accepted", "company-wide", "jobs.example.test"):
            self.assertIn(expected, html)
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;alert(1)", html)

    def test_state_filter_overrides_the_open_queue_default(self):
        response = self.client.get(self.url, {"state__exact": "accepted"})
        states = {row.state for row in response.context["cl"].result_list}
        self.assertEqual(states, {State.ACCEPTED})

    def _second_rto_claim(self, value="Hybrid, 2 days", source="https://other.example.test/about"):
        observation = CompanyProfileObservation.objects.get(organization=self.organization)
        return company_evidence.record_claim(
            self.organization,
            FieldKey.RTO_POLICY,
            value=value,
            observation=CompanyProfileObservation.objects.create(
                organization=self.organization,
                source_url=source,
                observed_domain="example.test",
                observed_name="Example Labs",
                rto_evidence=value,
                observed_at=observation.observed_at,
                extraction_version="test",
                status=CompanyProfileObservation.Status.CONFLICTED,
                fingerprint=f"fp-{value}",
            ),
            state=State.PENDING,
        )

    def test_bulk_accept_of_two_claims_for_one_field_is_refused(self):
        other = self._second_rto_claim()
        messages = []
        response = self._post_action("accept_claims", [self.rto.pk, other.pk])
        self.assertEqual(response.status_code, 302)
        self.rto.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.rto.state, State.PENDING)
        self.assertEqual(other.state, State.PENDING)
        self.assertFalse(OperationalChangeAudit.objects.exists())
        self.assertEqual(messages, [])

    def test_accepting_one_claim_audits_what_it_superseded(self):
        first = self._second_rto_claim()
        self._post_action("accept_claims", [self.rto.pk])
        self._post_action("accept_claims", [first.pk])
        audit = OperationalChangeAudit.objects.get(
            action="claim_accepted", target_id=str(first.pk)
        )
        self.assertEqual(audit.new_value["superseded"], [self.rto.pk])

    def test_scope_edit_keeps_claimed_domain_and_documents_matching(self):
        self._change(self.rto, {"countries": ["Germany"], "claimed_domain": "evil.test"})
        self.rto.refresh_from_db()
        self.assertEqual(
            self.rto.scope_json, {"countries": ["Germany"], "claimed_domain": "example.test"}
        )
        audit = OperationalChangeAudit.objects.get(action="scope_change")
        self.assertFalse(audit.confirmed)
        form = self.client.get(
            reverse("admin:crank_companyfieldevidence_change", args=[self.rto.pk])
        )
        self.assertContains(form, "whole-word")

    def test_scope_save_after_the_claim_closed_reports_an_error_and_writes_nothing(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from crank.admin import CompanyFieldEvidenceAdmin

        stale = CompanyFieldEvidence.objects.get(pk=self.rto.pk)
        stale.scope_json = {"countries": ["Germany"]}
        CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(state=State.SUPERSEDED)
        request = RequestFactory().post("/admin/")
        request.user = self.staff
        model_admin = CompanyFieldEvidenceAdmin(CompanyFieldEvidence, AdminSite())
        messages = []
        model_admin.message_user = lambda req, msg, level=None: messages.append((msg, level))
        model_admin.save_model(request, stale, None, True)
        self.assertEqual(len(messages), 1)
        self.assertIn("Scope not saved", messages[0][0])
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.scope_json, {"claimed_domain": "example.test"})
        self.assertFalse(OperationalChangeAudit.objects.filter(action="scope_change").exists())

    def _new_observation_review(self, action, value="Five days in office"):
        crawl_company_profile(self.source, client=FakeClient([profile(rto_evidence=value)]))
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        url = reverse("admin:crank_companyprofileobservation_changelist")
        self.client.post(
            url,
            {"action": action, ACTION_CHECKBOX_NAME: [observation.pk], "index": 0, "confirm": "yes"},
        )
        observation.refresh_from_db()
        return observation

    def test_rejecting_an_observation_rejects_its_open_claims(self):
        self._post_action("accept_claims", [self.rto.pk])
        observation = self._new_observation_review("reject_observations")
        claim = CompanyFieldEvidence.objects.get(
            organization=self.organization,
            field_key=FieldKey.RTO_POLICY,
            value_text="Five days in office",
        )
        self.assertEqual(claim.state, State.REJECTED)
        audit = OperationalChangeAudit.objects.get(
            target_type="company_profile_observation", target_id=str(observation.pk)
        )
        self.assertIn(claim.pk, audit.new_value["claims_resolved"])

    def test_accepting_an_observation_supersedes_its_claims_and_audits_ids(self):
        self._post_action("accept_claims", [self.rto.pk])
        observation = self._new_observation_review("accept_observations")
        self.assertEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)
        claim = CompanyFieldEvidence.objects.get(
            organization=self.organization,
            field_key=FieldKey.RTO_POLICY,
            value_text="Five days in office",
            state=State.SUPERSEDED,
        )
        audit = OperationalChangeAudit.objects.get(
            target_type="company_profile_observation", target_id=str(observation.pk)
        )
        self.assertIn(claim.pk, audit.new_value["claims_resolved"])
        self.assertIn(self.rto.pk, audit.new_value["evidence_superseded"])
        created = audit.new_value["evidence_created"]
        self.assertTrue(created)
        self.assertEqual(
            OperationalChangeAudit.objects.filter(
                action="observation_accepted", target_id__in=[str(pk) for pk in created]
            ).count(),
            len(created),
        )
        self.assertEqual(
            CompanyFieldEvidence.objects.get(
                organization=self.organization,
                field_key=FieldKey.RTO_POLICY,
                state=State.ACCEPTED,
            ).value_text,
            "Five days in office",
        )

    def test_accepting_an_observation_that_would_replace_scoped_evidence_is_refused(self):
        self._post_action("accept_claims", [self.rto.pk])
        CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(
            scope_json={"countries": ["Germany"], "claimed_domain": "example.test"}
        )
        observation = self._new_observation_review("accept_observations")
        self.assertNotEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.ACCEPTED)
        self.assertFalse(
            OperationalChangeAudit.objects.filter(
                target_type="company_profile_observation", target_id=str(observation.pk)
            ).exists()
        )


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
