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
