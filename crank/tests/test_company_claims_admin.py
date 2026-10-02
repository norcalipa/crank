# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Staff review of company evidence claims: admin, audit, scope, and the crawl-to-API path."""

import json
from unittest import mock

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
            preview = self.client.post(self.url, data)
            context = getattr(preview, "context", None)
            if context is not None and "extra_hidden" in context:
                data.update(dict(context["extra_hidden"]))
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
        model_admin = CompanyFieldEvidenceAdmin(CompanyFieldEvidence, AdminSite())
        selected = CompanyFieldEvidence.objects.filter(pk__in=[accepted.pk, self.rto.pk])
        request = RequestFactory().post(
            self.url,
            {"confirm": "yes", "claim_digest": model_admin.claims_digest(list(selected))},
        )
        request.user = self.staff
        messages = []
        model_admin.message_user = lambda _r, text, **_k: messages.append(text)
        model_admin.reject_claims(request, selected)
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
        preview = self.client.post(url, data)
        self.assertTemplateUsed(preview, "admin/confirm_action.html")
        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.AUTO_APPLIED)
        data.update(dict(preview.context["extra_hidden"]))
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
        self._post_observation_action(action, [observation.pk])
        observation.refresh_from_db()
        return observation

    def _post_observation_action(self, action, pks, **extra):
        url = reverse("admin:crank_companyprofileobservation_changelist")
        data = {"action": action, ACTION_CHECKBOX_NAME: pks, "index": 0, **extra}
        preview = self.client.post(url, data)
        data.update(dict(preview.context["extra_hidden"]))
        data["confirm"] = "yes"
        return self.client.post(url, data, follow=True)

    def test_rejecting_an_observation_supersedes_its_open_claims(self):
        self._post_action("accept_claims", [self.rto.pk])
        observation = self._new_observation_review("reject_observations")
        claim = CompanyFieldEvidence.objects.get(
            organization=self.organization,
            field_key=FieldKey.RTO_POLICY,
            value_text="Five days in office",
        )
        self.assertEqual(claim.state, State.SUPERSEDED)
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

    def _messages(self, response):
        return [str(m) for m in response.context["messages"]] if response.context else []

    def _make_legacy(self):
        """Turn the RTO claim into a never-reviewed accepted (pre-#474) row."""
        CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(state=State.ACCEPTED)
        self.rto.refresh_from_db()
        return self.rto

    def test_select_across_is_refused_for_claim_actions(self):
        response = self.client.post(
            self.url,
            {
                "action": "accept_claims",
                "select_across": "1",
                ACTION_CHECKBOX_NAME: [self.rto.pk],
                "index": 0,
                "confirm": "yes",
            },
            follow=True,
        )
        self.assertTrue(any("select" in m.lower() for m in self._messages(response)))
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.PENDING)
        self.assertFalse(OperationalChangeAudit.objects.exists())

    def test_confirmation_bound_to_other_claims_is_refused(self):
        other = self._second_rto_claim()
        data = {"action": "accept_claims", ACTION_CHECKBOX_NAME: [self.rto.pk], "index": 0}
        preview = self.client.post(self.url, data)
        digest = dict(preview.context["extra_hidden"])["claim_digest"]
        other_preview = self.client.post(
            self.url, {**data, ACTION_CHECKBOX_NAME: [other.pk]}
        )
        wrong = dict(other_preview.context["extra_hidden"])["claim_digest"]
        self.assertNotEqual(digest, wrong)

        response = self.client.post(
            self.url, {**data, "confirm": "yes", "claim_digest": wrong}, follow=True
        )
        self.assertTrue(any("No changes made" in m for m in self._messages(response)))
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.PENDING)

        changed = self.client.post(self.url, data)
        digest = dict(changed.context["extra_hidden"])["claim_digest"]
        CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(value_text="Swapped")
        response = self.client.post(
            self.url, {**data, "confirm": "yes", "claim_digest": digest}, follow=True
        )
        self.assertTrue(any("No changes made" in m for m in self._messages(response)))
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.PENDING)

    def test_a_missing_claim_digest_is_refused(self):
        self.client.post(
            self.url,
            {"action": "accept_claims", ACTION_CHECKBOX_NAME: [self.rto.pk], "index": 0,
             "confirm": "yes"},
        )
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.PENDING)

    def test_rejecting_a_legacy_claim_retracts_the_row_and_matching(self):
        legacy = self._make_legacy()
        self.assertTrue(company_evidence.legacy_unreviewed_rows(self.organization))
        claim = company_evidence.queue_legacy_claim(legacy)
        self._post_action("reject_claims", [claim.pk])
        legacy.refresh_from_db()
        self.assertEqual(legacy.state, State.SUPERSEDED)
        self.assertNotIn(
            FieldKey.RTO_POLICY, company_evidence.resolve_field_evidence(self.organization)
        )

    def test_legacy_filter_and_relation_column(self):
        legacy = self._make_legacy()
        response = self.client.get(self.url, {"legacy": "unreviewed", "queue": "all"})
        self.assertEqual({r.pk for r in response.context["cl"].result_list}, {legacy.pk})
        claim = company_evidence.queue_legacy_claim(legacy)
        model_admin = admin_site_registry(CompanyFieldEvidence)
        self.assertEqual(model_admin.relation_to_accepted(claim), "legacy value in effect")
        self.assertEqual(model_admin.relation_to_accepted(legacy), "")
        differing = self._second_rto_claim("Something else")
        self.assertEqual(model_admin.relation_to_accepted(differing), "differs from accepted")
        label = model_admin.confirmation_label(claim)
        self.assertIn("legacy value already in effect", label)

    def test_confirmation_shows_scope_changes(self):
        legacy = self._make_legacy()
        CompanyFieldEvidence.objects.filter(pk=legacy.pk).update(
            scope_json={"countries": ["Germany"], "claimed_domain": "example.test"}
        )
        claim = company_evidence.queue_legacy_claim(CompanyFieldEvidence.objects.get(pk=legacy.pk))
        model_admin = admin_site_registry(CompanyFieldEvidence)
        self.assertIn("scope {'countries': ['Germany']}", model_admin.confirmation_label(claim))
        CompanyFieldEvidence.objects.filter(pk=claim.pk).update(
            scope_json={"countries": []}
        )
        claim.refresh_from_db()
        self.assertIn("CHANGES the accepted scope", model_admin.confirmation_label(claim))

    def test_accepting_a_claim_for_the_accepted_value_keeps_its_scope(self):
        legacy = self._make_legacy()
        CompanyFieldEvidence.objects.filter(pk=legacy.pk).update(
            scope_json={"countries": ["Germany"], "claimed_domain": "example.test"}
        )
        claim = company_evidence.queue_legacy_claim(CompanyFieldEvidence.objects.get(pk=legacy.pk))
        self._post_action("accept_claims", [claim.pk])
        accepted = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.RTO_POLICY,
            state=State.ACCEPTED,
        )
        self.assertEqual(accepted.scope_json.get("countries"), ["Germany"])

    def test_refused_scope_save_reports_no_success_and_logs_nothing(self):
        from django.contrib.admin.models import LogEntry

        CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(state=State.SUPERSEDED)
        url = reverse("admin:crank_companyfieldevidence_change", args=[self.rto.pk])
        before = LogEntry.objects.count()
        response = self.client.post(
            url, {"scope_json": json.dumps({"countries": ["Germany"]})}, follow=True
        )
        self.assertContains(response, "Scope not saved")
        self.assertEqual(LogEntry.objects.count(), before)
        self.assertFalse(
            any("changed successfully" in m for m in self._messages(response))
        )
        self.assertFalse(OperationalChangeAudit.objects.filter(action="scope_change").exists())

    def test_race_refused_scope_save_suppresses_log_and_success_message(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from crank.admin import CompanyFieldEvidenceAdmin

        stale = CompanyFieldEvidence.objects.get(pk=self.rto.pk)
        stale.scope_json = {"countries": ["Germany"]}
        CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(state=State.SUPERSEDED)
        request = RequestFactory().post("/admin/")
        request.user = self.staff
        model_admin = CompanyFieldEvidenceAdmin(CompanyFieldEvidence, AdminSite())
        model_admin.message_user = lambda *a, **k: None
        model_admin.save_model(request, stale, None, True)
        self.assertTrue(request._scope_save_refused)
        self.assertIsNone(model_admin.log_change(request, stale, "changed"))
        response = model_admin.response_change(request, stale)
        self.assertEqual(response.status_code, 302)

    def test_queue_filter_marks_the_effective_selection(self):
        response = self.client.get(self.url, {"state__exact": "accepted"})
        choices = list(
            next(
                f for f in response.context["cl"].filter_specs
                if f.__class__.__name__ == "OpenClaimFilter"
            ).choices(response.context["cl"])
        )
        self.assertFalse(any(c["selected"] for c in choices))
        default = self.client.get(self.url)
        choices = list(
            next(
                f for f in default.context["cl"].filter_specs
                if f.__class__.__name__ == "OpenClaimFilter"
            ).choices(default.context["cl"])
        )
        self.assertEqual(sum(1 for c in choices if c["selected"]), 1)

    def test_empty_selection_reports_no_changes(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from crank.admin import CompanyFieldEvidenceAdmin

        model_admin = CompanyFieldEvidenceAdmin(CompanyFieldEvidence, AdminSite())
        request = RequestFactory().post(self.url, {"confirm": "yes"})
        request.user = self.staff
        messages = []
        model_admin.message_user = lambda _r, text, **_k: messages.append(text)
        model_admin.accept_claims(request, CompanyFieldEvidence.objects.none())
        self.assertEqual(len(messages), 1)
        self.assertIn("No changes made", messages[0])

    def test_accepting_an_observation_with_a_rejected_value_is_refused(self):
        self._post_action("accept_claims", [self.rto.pk])
        crawl_company_profile(
            self.source, client=FakeClient([profile(rto_evidence="Hybrid")]),
        )
        claim = CompanyFieldEvidence.objects.get(value_text="Hybrid", state__in=company_evidence.OPEN_CLAIM_STATES)
        self._post_action("reject_claims", [claim.pk])
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization, rto_evidence="Hybrid"
        ).latest("id")
        url = reverse("admin:crank_companyprofileobservation_changelist")
        response = self.client.post(
            url,
            {"action": "accept_observations", ACTION_CHECKBOX_NAME: [observation.pk],
             "index": 0, "confirm": "yes"},
            follow=True,
        )
        observation.refresh_from_db()
        self.assertNotEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)
        self.assertTrue(any("reject" in m.lower() for m in self._messages(response)))
        self.assertEqual(
            CompanyFieldEvidence.objects.get(
                organization=self.organization, field_key=FieldKey.RTO_POLICY,
                state=State.ACCEPTED,
            ).pk,
            self.rto.pk,
        )

    def test_observation_confirmation_lists_carried_values(self):
        self._post_action("accept_claims", [self.rto.pk])
        crawl_company_profile(
            self.source, client=FakeClient([profile(rto_evidence="Office five days")]),
        )
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        url = reverse("admin:crank_companyprofileobservation_changelist")
        response = self.client.post(
            url,
            {"action": "accept_observations", ACTION_CHECKBOX_NAME: [observation.pk], "index": 0},
        )
        self.assertContains(response, "Office five days")
        self.assertContains(response, "verifies policy fact")

    def test_accepting_an_observation_restates_other_pages_claims(self):
        self._post_action("accept_claims", [self.rto.pk])
        twin = self._second_rto_claim("Five days in office", "https://other.example.test/about")
        observation = self._new_observation_review("accept_observations")
        twin.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)
        self.assertEqual(twin.state, State.SUPERSEDED)
        audit = OperationalChangeAudit.objects.get(
            target_type="company_profile_observation", target_id=str(observation.pk)
        )
        self.assertIn(twin.pk, audit.new_value["claims_closed"])

    def test_locked_scope_refusal_rolls_back_the_observation_accept(self):
        from unittest import mock

        self._post_action("accept_claims", [self.rto.pk])
        with mock.patch.object(
            company_evidence,
            "accept_observation_fields",
            side_effect=company_evidence.EvidenceNotAcceptable("scoped"),
        ):
            observation = self._new_observation_review("accept_observations")
        self.assertNotEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)
        self.assertFalse(
            OperationalChangeAudit.objects.filter(
                target_type="company_profile_observation", target_id=str(observation.pk)
            ).exists()
        )
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.ACCEPTED)

    def test_observation_label_shows_kept_scope_and_handles_unresolved_identity(self):
        self._post_action("accept_claims", [self.rto.pk])
        CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(
            scope_json={"countries": ["Germany"], "claimed_domain": "example.test"}
        )
        crawl_company_profile(self.source, client=FakeClient([profile()]))
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        model_admin = admin_site_registry(CompanyProfileObservation)
        self.assertIn("keeps scope", model_admin.confirmation_label(observation))
        observation.organization = None
        label = model_admin.confirmation_label(observation)
        self.assertNotIn("accepting records", label)

    def test_relation_column_for_a_reviewed_accepted_value(self):
        self._post_action("accept_claims", [self.rto.pk])
        twin = CompanyFieldEvidence(
            organization=self.organization,
            field_key=FieldKey.RTO_POLICY,
            value_text=self.rto.value_text,
            state=State.PENDING,
        )
        model_admin = admin_site_registry(CompanyFieldEvidence)
        self.assertEqual(model_admin.relation_to_accepted(twin), "matches reviewed value")

    def test_queueing_skips_rows_that_are_not_legacy(self):
        self._post_action("accept_claims", [self.rto.pk])
        reviewed = CompanyFieldEvidence.objects.get(pk=self.rto.pk)
        self.assertIsNone(company_evidence.queue_legacy_claim(reviewed))

    def test_legacy_evidence_review_command_lists_and_queues(self):
        from io import StringIO

        from django.core.management import call_command

        legacy = self._make_legacy()
        out = StringIO()
        call_command("legacy_evidence_review", stdout=out)
        self.assertIn(str(legacy.pk), out.getvalue())
        self.assertIn("1 legacy unreviewed row(s); 0 claim(s) queued.", out.getvalue())
        out = StringIO()
        call_command("legacy_evidence_review", "--queue", stdout=out)
        self.assertIn("1 claim(s) queued.", out.getvalue())
        out = StringIO()
        call_command("legacy_evidence_review", "--queue", stdout=out)
        self.assertIn("0 claim(s) queued.", out.getvalue())
        self.assertEqual(
            CompanyFieldEvidence.objects.filter(
                field_key=FieldKey.RTO_POLICY, state=State.PENDING
            ).count(),
            1,
        )

    # Adversarial-review round 3 (#474): observation admin, legacy filter, bulk scope.

    def test_select_across_accept_of_observations_is_refused(self):
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        CompanyProfileObservation.objects.filter(pk=observation.pk).update(
            status=CompanyProfileObservation.Status.CONFLICTED
        )
        url = reverse("admin:crank_companyprofileobservation_changelist")
        response = self.client.post(
            url,
            {"action": "accept_observations", "select_across": "1",
             ACTION_CHECKBOX_NAME: [observation.pk], "index": 0, "confirm": "yes"},
            follow=True,
        )
        self.assertTrue(any("select" in m.lower() for m in self._messages(response)))
        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.CONFLICTED)
        self.assertFalse(OperationalChangeAudit.objects.exists())

    def test_observation_accept_is_bound_to_the_values_it_replaces(self):
        self._post_action("accept_claims", [self.rto.pk])
        crawl_company_profile(
            self.source, client=FakeClient([profile(rto_evidence="Five days, no exceptions")])
        )
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        url = reverse("admin:crank_companyprofileobservation_changelist")
        data = {"action": "accept_observations", ACTION_CHECKBOX_NAME: [observation.pk], "index": 0}
        preview = self.client.post(url, data)
        label = preview.context["objects"][0].confirmation_label
        self.assertIn("REPLACES 'Remote first' (staff-reviewed)", label)
        digest = dict(preview.context["extra_hidden"])["observation_digest"]
        # The reviewed value changes between preview and confirm.
        CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(value_text="Hybrid")
        response = self.client.post(
            url, {**data, "confirm": "yes", "observation_digest": digest}, follow=True
        )
        self.assertTrue(any("No changes made" in m for m in self._messages(response)))
        observation.refresh_from_db()
        self.assertNotEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)
        # No digest at all is refused the same way.
        response = self.client.post(url, {**data, "confirm": "yes"}, follow=True)
        self.assertTrue(any("No changes made" in m for m in self._messages(response)))

    def test_rejecting_an_accepted_observation_withdraws_the_facts_it_verified(self):
        observation = self._new_observation_review("accept_observations", value="Five days")
        self.assertEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)
        rto = CompanyFieldEvidence.objects.get(
            organization=self.organization, field_key=FieldKey.RTO_POLICY, state=State.ACCEPTED
        )
        self.assertTrue(company_evidence.is_staff_reviewed(rto))
        self._post_observation_action("reject_observations", [observation.pk])
        rto.refresh_from_db()
        self.assertEqual(rto.state, State.SUPERSEDED)
        audit = OperationalChangeAudit.objects.get(
            target_type="company_profile_observation", action="review_rejected"
        )
        self.assertIn(rto.pk, audit.new_value["evidence_retracted"])

    def test_bare_legacy_link_lists_the_rows_with_a_bounded_query_count(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        legacy = self._make_legacy()
        response = self.client.get(self.url, {"legacy": "unreviewed"})
        self.assertEqual({r.pk for r in response.context["cl"].result_list}, {legacy.pk})
        for index in range(8):
            CompanyFieldEvidence.objects.create(
                organization=self.organization, field_key=FieldKey.FUNDING_ROUND,
                value_text=f"Series {index}", source_url=f"https://o.example.test/{index}",
                source_domain="o.example.test", observed_at=legacy.observed_at,
                state=State.PENDING,
            )
        with CaptureQueriesContext(connection) as few:
            self.client.get(self.url, {"queue": "all"})
        for index in range(8, 30):
            CompanyFieldEvidence.objects.create(
                organization=self.organization, field_key=FieldKey.FUNDING_ROUND,
                value_text=f"Series {index}", source_url=f"https://o.example.test/{index}",
                source_domain="o.example.test", observed_at=legacy.observed_at,
                state=State.PENDING,
            )
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(self.url, {"queue": "all"})
        self.assertEqual(len(many), len(few))
        self.assertIn(
            "no accepted value",
            [
                admin_site_registry(CompanyFieldEvidence).relation_to_accepted(r)
                for r in response.context["cl"].result_list
            ],
        )

    def test_bulk_accept_of_one_value_with_different_scopes_is_refused(self):
        other = self._second_rto_claim(self.rto.value_text, source="https://jobs.example.test/b")
        CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(
            scope_json={"countries": ["Germany"], "claimed_domain": "example.test"}
        )
        data = {"action": "accept_claims", ACTION_CHECKBOX_NAME: [self.rto.pk, other.pk], "index": 0}
        preview = self.client.post(self.url, data)
        data.update(dict(preview.context["extra_hidden"]))
        response = self.client.post(self.url, {**data, "confirm": "yes"}, follow=True)
        self.assertTrue(any("different scopes" in m for m in self._messages(response)))
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.PENDING)
        other.refresh_from_db()
        self.assertIn(other.state, company_evidence.OPEN_CLAIM_STATES)

    def test_changelist_column_marks_a_twin_of_a_reviewed_value(self):
        self._post_action("accept_claims", [self.rto.pk])
        twin = CompanyFieldEvidence.objects.create(
            organization=self.organization, field_key=FieldKey.RTO_POLICY,
            value_text=self.rto.value_text, source_url="https://jobs.example.test/twin",
            source_domain="jobs.example.test", observed_at=self.rto.observed_at,
            state=State.PENDING,
        )
        response = self.client.get(self.url)
        listed = {r.pk: r for r in response.context["cl"].result_list}
        self.assertEqual(listed[twin.pk]._vs_accepted, "matches reviewed value")

    # Security round 1 (#474).

    def _legacy_with_observation(self):
        legacy = self._make_legacy()
        CompanyProfileObservation.objects.filter(pk=legacy.observation_id).update(
            status=CompanyProfileObservation.Status.AUTO_APPLIED
        )
        return legacy

    def test_observation_status_cannot_be_edited_on_the_change_form(self):
        legacy = self._legacy_with_observation()
        url = reverse(
            "admin:crank_companyprofileobservation_change", args=[legacy.observation_id]
        )
        response = self.client.get(url)
        self.assertNotIn("status", response.context["adminform"].form.fields)
        self.client.post(url, {"status": "accepted", "admin_note": "sneaky"})
        observation = CompanyProfileObservation.objects.get(pk=legacy.observation_id)
        self.assertEqual(observation.status, CompanyProfileObservation.Status.AUTO_APPLIED)
        self.assertFalse(company_evidence.is_staff_reviewed(legacy))
        self.assertTrue(company_evidence.legacy_unreviewed_rows(self.organization))

    def test_audit_admin_is_view_only(self):
        OperationalChangeAudit.record(
            actor=self.staff, target_type="company_field_evidence", target_id="1",
            action="claim_accepted", confirmed=True,
        )
        audit_admin = admin_site_registry(OperationalChangeAudit)
        request = self.client.get(self.url).wsgi_request
        self.assertFalse(audit_admin.has_add_permission(request))
        self.assertFalse(audit_admin.has_change_permission(request))
        self.assertFalse(audit_admin.has_delete_permission(request))
        self.assertTrue(audit_admin.has_view_permission(request))
        self.assertNotIn("delete_selected", audit_admin.get_actions(request))
        changelist = reverse("admin:crank_operationalchangeaudit_changelist")
        pk = OperationalChangeAudit.objects.get().pk
        self.client.post(
            changelist,
            {"action": "delete_selected", ACTION_CHECKBOX_NAME: [pk], "post": "yes"},
        )
        self.client.post(
            reverse("admin:crank_operationalchangeaudit_delete", args=[pk]), {"post": "yes"}
        )
        self.assertTrue(OperationalChangeAudit.objects.filter(pk=pk).exists())

    def test_rejecting_a_legacy_auto_applied_observation_withdraws_its_facts(self):
        legacy = self._legacy_with_observation()
        observation = CompanyProfileObservation.objects.get(pk=legacy.observation_id)
        url = reverse("admin:crank_companyprofileobservation_changelist")
        preview = self.client.post(
            url,
            {"action": "reject_observations", ACTION_CHECKBOX_NAME: [observation.pk], "index": 0},
        )
        label = preview.context["objects"][0].confirmation_label
        self.assertIn("WITHDRAWS", label)
        self.assertIn("rto_policy=", label)
        self._post_observation_action("reject_observations", [observation.pk])
        legacy.refresh_from_db()
        self.assertEqual(legacy.state, State.SUPERSEDED)
        self.assertNotIn(
            FieldKey.RTO_POLICY, company_evidence.resolve_field_evidence(self.organization)
        )
        audit = OperationalChangeAudit.objects.get(
            target_type="company_profile_observation", action="review_rejected"
        )
        self.assertIn(legacy.pk, audit.new_value["evidence_retracted"])

    def test_reject_label_for_an_observation_with_nothing_to_withdraw(self):
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        observation_admin = admin_site_registry(CompanyProfileObservation)
        CompanyProfileObservation.objects.filter(pk=observation.pk).update(
            status=CompanyProfileObservation.Status.PENDING
        )
        observation.refresh_from_db()
        pending_label = observation_admin.confirmation_label_for_action(
            observation, "reject_observations"
        )
        self.assertNotIn("WITHDRAWS", pending_label)
        self.assertNotIn("accepting records", pending_label)
        CompanyProfileObservation.objects.filter(pk=observation.pk).update(
            status=CompanyProfileObservation.Status.ACCEPTED
        )
        observation.refresh_from_db()
        CompanyFieldEvidence.objects.filter(observation=observation).update(
            state=State.SUPERSEDED
        )
        self.assertIn(
            "withdraws no verified facts",
            observation_admin.confirmation_label_for_action(observation, "reject_observations"),
        )
        self.assertNotIn(
            "accepting records",
            observation_admin.confirmation_label_for_action(observation, "conflict_observations"),
        )

    def test_select_across_reject_of_observations_is_refused(self):
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        CompanyProfileObservation.objects.filter(pk=observation.pk).update(
            status=CompanyProfileObservation.Status.ACCEPTED
        )
        url = reverse("admin:crank_companyprofileobservation_changelist")
        response = self.client.post(
            url,
            {"action": "reject_observations", "select_across": "1",
             ACTION_CHECKBOX_NAME: [observation.pk], "index": 0, "confirm": "yes"},
            follow=True,
        )
        self.assertTrue(any("select-across" in m for m in self._messages(response)))
        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)

    def test_claim_digest_is_checked_after_the_organization_lock(self):
        data = {"action": "accept_claims", ACTION_CHECKBOX_NAME: [self.rto.pk], "index": 0}
        preview = self.client.post(self.url, data)
        data.update(dict(preview.context["extra_hidden"]))
        real_lock = company_evidence.lock_organizations

        def lock_after_a_rival_change(ids):
            CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(value_text="Hybrid")
            real_lock(ids)

        with mock.patch.object(company_evidence, "lock_organizations", lock_after_a_rival_change):
            response = self.client.post(self.url, {**data, "confirm": "yes"}, follow=True)
        self.assertTrue(any("No changes made" in m for m in self._messages(response)))
        self.rto.refresh_from_db()
        self.assertEqual(self.rto.state, State.PENDING)

    def test_observation_digest_is_checked_after_the_organization_lock(self):
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        url = reverse("admin:crank_companyprofileobservation_changelist")
        data = {"action": "accept_observations", ACTION_CHECKBOX_NAME: [observation.pk], "index": 0}
        preview = self.client.post(url, data)
        data.update(dict(preview.context["extra_hidden"]))
        real_lock = company_evidence.lock_organizations

        def lock_after_a_rival_change(ids):
            CompanyProfileObservation.objects.filter(pk=observation.pk).update(
                status=CompanyProfileObservation.Status.CONFLICTED
            )
            real_lock(ids)

        with mock.patch.object(company_evidence, "lock_organizations", lock_after_a_rival_change):
            response = self.client.post(url, {**data, "confirm": "yes"}, follow=True)
        self.assertTrue(any("No changes made" in m for m in self._messages(response)))
        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.CONFLICTED)

    def _observation_post(self, action, pks):
        url = reverse("admin:crank_companyprofileobservation_changelist")
        data = {"action": action, ACTION_CHECKBOX_NAME: pks, "index": 0}
        preview = self.client.post(url, data)
        data.update(dict(preview.context["extra_hidden"]))
        return url, {**data, "confirm": "yes"}

    def _rival_accept_at_lock(self, observation):
        real_lock = company_evidence.lock_organizations

        def lock_after_a_rival_accept(ids):
            real_lock(ids)
            CompanyProfileObservation.objects.filter(pk=observation.pk).update(
                status=CompanyProfileObservation.Status.ACCEPTED
            )
            observation.refresh_from_db()
            company_evidence.accept_observation_fields(observation)

        return mock.patch.object(company_evidence, "lock_organizations", lock_after_a_rival_accept)

    def _pending_observation(self):
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        CompanyFieldEvidence.objects.filter(observation=observation).update(
            state=State.PENDING
        )
        CompanyProfileObservation.objects.filter(pk=observation.pk).update(
            status=CompanyProfileObservation.Status.PENDING
        )
        observation.refresh_from_db()
        return observation

    def test_reject_refuses_when_a_rival_accept_lands_after_the_preview(self):
        observation = self._pending_observation()
        url, data = self._observation_post("reject_observations", [observation.pk])
        with self._rival_accept_at_lock(observation):
            response = self.client.post(url, data, follow=True)
        self.assertTrue(any("No changes made" in m for m in self._messages(response)))
        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)
        accepted = CompanyFieldEvidence.objects.filter(
            observation=observation, state=State.ACCEPTED
        )
        self.assertTrue(accepted.exists())
        self.assertFalse(
            OperationalChangeAudit.objects.filter(
                target_type="company_profile_observation", action="review_rejected"
            ).exists()
        )

    def test_mark_conflicted_refuses_when_a_rival_accept_lands_after_the_preview(self):
        observation = self._pending_observation()
        url, data = self._observation_post("conflict_observations", [observation.pk])
        with self._rival_accept_at_lock(observation):
            response = self.client.post(url, data, follow=True)
        self.assertTrue(any("No changes made" in m for m in self._messages(response)))
        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)

    def test_decisions_lock_organizations_in_pk_order_whatever_the_page_order(self):
        first = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        other = Organization.objects.create(name="Other Labs", url="https://other.test")
        second = CompanyProfileObservation.objects.get(pk=first.pk)
        second.pk = None
        second.organization = other
        second.fingerprint = "other-fingerprint"
        second.save()
        self.assertLess(self.organization.pk, other.pk)
        for action in ("reject_observations", "conflict_observations", "accept_observations"):
            url, data = self._observation_post(action, [second.pk, first.pk])
            locked = []
            with mock.patch.object(
                company_evidence, "_lock_organization", side_effect=locked.append
            ):
                self.client.post(url, data, follow=True)
            self.assertEqual(locked[:2], [self.organization.pk, other.pk], action)

    def test_confirmation_labels_neutralise_bidi_and_zero_width_characters(self):
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        CompanyProfileObservation.objects.filter(pk=observation.pk).update(
            observed_name="Ex\u202eample\u200b Labs", source_url="https://x.test/\u2066a"
        )
        Organization.objects.filter(pk=self.organization.pk).update(name="Ex\u202eample Labs")
        observation.refresh_from_db()
        self.rto.refresh_from_db()
        labels = [
            admin_site_registry(CompanyProfileObservation).confirmation_label(observation),
            admin_site_registry(CompanyProfileObservation).confirmation_label_for_action(
                observation, "reject_observations"
            ),
            admin_site_registry(CompanyFieldEvidence).confirmation_label(self.rto),
        ]
        for label in labels:
            for char in ("\u202e", "\u200b", "\u2066"):
                self.assertNotIn(char, label)
        self.assertIn("Example Labs", labels[0])

    def test_claim_views_show_how_matching_reads_the_value(self):
        CompanyFieldEvidence.objects.filter(pk=self.rto.pk).update(value_text="Remote first")
        self.rto.refresh_from_db()
        claim_admin = admin_site_registry(CompanyFieldEvidence)
        self.assertIn("remote (0 in-office days)", claim_admin.confirmation_label(self.rto))
        self.assertIn("remote (0 in-office days)", claim_admin.matching_reading(self.rto))
        self.assertEqual(
            company_evidence.matching_reading(FieldKey.PUBLIC_STATUS, "Private company"),
            "matching reads this as: private",
        )
        self.assertEqual(
            company_evidence.matching_reading(FieldKey.PUBLIC_STATUS, "Public company"),
            "matching reads this as: public",
        )
        self.assertIn(
            "cannot read", company_evidence.matching_reading(FieldKey.PUBLIC_STATUS, "unclear")
        )
        self.assertIn(
            "cannot read", company_evidence.matching_reading(FieldKey.RTO_POLICY, "TBD")
        )
        self.assertEqual(company_evidence.matching_reading(FieldKey.FUNDING_ROUND, "Series B"), "")
        observation = CompanyProfileObservation.objects.filter(
            organization=self.organization
        ).latest("id")
        label = admin_site_registry(CompanyProfileObservation).confirmation_label(observation)
        self.assertIn("matching reads this as", label)

    def test_legacy_command_strips_terminal_escapes_and_audits_the_queue(self):
        from io import StringIO

        from django.core.management import call_command

        legacy = self._make_legacy()
        CompanyFieldEvidence.objects.filter(pk=legacy.pk).update(
            value_text="Remote\x1b[2K\r5 days forged row"
        )
        Organization.objects.filter(pk=self.organization.pk).update(name="Ex\x1b[1Gample")
        out = StringIO()
        call_command("legacy_evidence_review", "--queue", stdout=out)
        self.assertNotIn("\x1b", out.getvalue())
        self.assertNotIn("\r", out.getvalue())
        audit = OperationalChangeAudit.objects.get(action="legacy_queue")
        self.assertEqual(audit.new_value["claims_queued"], 1)
        self.assertEqual(len(audit.new_value["claim_ids"]), 1)
        call_command("legacy_evidence_review", stdout=StringIO())
        self.assertEqual(OperationalChangeAudit.objects.filter(action="legacy_queue").count(), 1)


def admin_site_registry(model):
    from django.contrib import admin

    return admin.site._registry[model]


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
