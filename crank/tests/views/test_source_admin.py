# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import TestCase
from unittest.mock import patch

from crank.admin import SourceCatalogAdmin, SourceCatalogAuditAdmin, SourceRunAdmin
from crank.models.organization import Organization
from crank.models.source import (
    ApprovalState,
    SourceCatalog,
    SourceCatalogAudit,
    SourceRun,
)


class MockRequest:
    def __init__(self, user=None):
        self.user = user or User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )


def make_source():
    org = Organization.objects.create(name="Ratings Org", gives_ratings=True, status=1)
    return SourceCatalog.objects.create(
        name="Ratings Org source",
        organization=org,
        adapter_key="fake.v1",
        base_url="https://ratings.example.test",
        approval_state=ApprovalState.APPROVED,
        enabled=True,
    )


class StaffOnlySourceAdminTests(TestCase):
    def _req(self, is_staff):
        return MockRequest(
            User.objects.create_user(
                username="u", password="pw", is_staff=is_staff
            )
        )

    def test_non_staff_cannot_access_source_admin(self):
        site = AdminSite()
        admin = SourceCatalogAdmin(SourceCatalog, site)
        req = self._req(is_staff=False)
        for result in (
            admin.has_add_permission(req),
            admin.has_view_permission(req, None),
            admin.has_change_permission(req, None),
            admin.has_delete_permission(req, None),
            admin.has_module_permission(req),
        ):
            self.assertFalse(result)

    def test_staff_can_access_source_admin(self):
        site = AdminSite()
        admin = SourceCatalogAdmin(SourceCatalog, site)
        req = self._req(is_staff=True)
        self.assertTrue(admin.has_view_permission(req, None))
        self.assertTrue(admin.has_add_permission(req))
        self.assertTrue(admin.has_change_permission(req, None))

    def test_non_staff_gets_empty_queryset(self):
        site = AdminSite()
        admin = SourceCatalogAdmin(SourceCatalog, site)
        req = self._req(is_staff=False)
        make_source()
        self.assertEqual(SourceCatalog.objects.count(), 1)
        self.assertEqual(admin.get_queryset(req).count(), 0)


class SourceAdminAuditTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.admin = SourceCatalogAdmin(SourceCatalog, self.site)
        self.user = User.objects.create_user(
            username="admin2", password="pw", is_staff=True
        )
        self.req = MockRequest(self.user)

    def test_save_model_change_records_audit(self):
        src = make_source()
        # Admin applies form values onto ``obj`` before ``save_model`` runs.
        src.enabled = False
        form = self._form_for(src)
        self.admin.save_model(self.req, src, form, change=True)
        src.refresh_from_db()
        self.assertFalse(src.enabled)
        audit = SourceCatalogAudit.objects.filter(source=src).order_by("-id").first()
        self.assertEqual(audit.action, SourceCatalogAudit.Action.CHANGED)
        self.assertEqual(audit.user, self.user)
        self.assertEqual(audit.changed_fields["enabled"], {"from": True, "to": False})

    def test_save_model_add_records_creation_audit(self):
        org = Organization.objects.create(
            name="Fresh Ratings Org", gives_ratings=True, status=1
        )
        src = SourceCatalog(
            name="Brand New",
            organization=org,
            adapter_key="fake.v1",
            base_url="https://ratings.example.test",
            approval_state=ApprovalState.PENDING,
            enabled=False,
        )
        form = self._form_for(src)
        self.admin.save_model(self.req, src, form, change=False)
        src.refresh_from_db()
        self.assertIsNotNone(src.pk)
        audit = SourceCatalogAudit.objects.filter(source=src).first()
        self.assertEqual(audit.action, SourceCatalogAudit.Action.CREATED)
        self.assertEqual(audit.user, self.user)

    def test_state_admin_action_approves_and_audits(self):
        src = make_source()
        src.approval_state = ApprovalState.PENDING
        src.enabled = False
        src.save(
            update_fields=["approval_state", "enabled"]
        )
        qs = SourceCatalog.objects.filter(pk=src.pk)
        with patch.object(self.admin, "message_user", lambda *a, **k: None):
            self.admin.approve_sources(self.req, qs)
        src.refresh_from_db()
        self.assertEqual(src.approval_state, ApprovalState.APPROVED)
        self.assertIsNotNone(src.approved_at)
        audit = SourceCatalogAudit.objects.filter(
            source=src, action=SourceCatalogAudit.Action.APPROVED
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.user, self.user)

    def test_admin_actions_record_block_enable_disable(self):
        src = make_source()
        with patch.object(self.admin, "message_user", lambda *a, **k: None):
            self.admin.block_sources(
                self.req, SourceCatalog.objects.filter(pk=src.pk)
            )
            self.admin.enable_sources(
                self.req, SourceCatalog.objects.filter(pk=src.pk)
            )
            self.admin.disable_sources(
                self.req, SourceCatalog.objects.filter(pk=src.pk)
            )
        src.refresh_from_db()
        self.assertEqual(src.approval_state, ApprovalState.BLOCKED)
        self.assertFalse(src.enabled)
        by_action = {
            a.action: a for a in SourceCatalogAudit.objects.filter(source=src)
        }
        for action in (
            SourceCatalogAudit.Action.BLOCKED,
            SourceCatalogAudit.Action.ENABLED,
            SourceCatalogAudit.Action.DISABLED,
        ):
            self.assertIn(action, by_action)

    def _form_for(self, src):
        class _Form:
            cleaned_data = {}
        return _Form()


class SourceRunAdminTests(TestCase):
    def test_non_staff_cannot_access_source_run_admin(self):
        site = AdminSite()
        admin = SourceRunAdmin(SourceRun, site)
        user = User.objects.create_user(username="u", password="pw", is_staff=False)
        req = MockRequest(user)
        self.assertFalse(admin.has_view_permission(req, None))
        self.assertFalse(admin.has_add_permission(req))

    def test_audit_admin_is_staff_only(self):
        site = AdminSite()
        admin = SourceCatalogAuditAdmin(SourceCatalogAudit, site)
        user = User.objects.create_user(username="u2", password="pw", is_staff=False)
        self.assertFalse(admin.has_view_permission(MockRequest(user), None))
