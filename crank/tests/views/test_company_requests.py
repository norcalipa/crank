# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import json
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.db import IntegrityError
from django.test import TestCase, Client, RequestFactory, override_settings
from django.urls import reverse

from crank.admin import CompanyRequestAdmin
from crank.models.company_request import CompanyRequest
from crank.models.monitoring import OperationalChangeAudit
from crank.models.organization import Organization
from crank.views.company_requests import _rate_limited
from django.contrib.admin.sites import AdminSite


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class CompanyRequestsViewTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser", password="testpass123", email="test@test.com"
        )
        self.other_user = User.objects.create_user(
            username="otheruser", password="testpass123", email="other@test.com"
        )

    def tearDown(self):
        cache.clear()

    def test_get_requires_auth(self):
        response = self.client.get("/api/company-requests/")
        self.assertEqual(response.status_code, 401)

    def test_get_list(self):
        self.client.force_login(self.user)
        CompanyRequest.objects.create(
            requester=self.user, company_name="Acme", website_url="https://acme.com"
        )
        CompanyRequest.objects.create(
            requester=self.user, company_name="Beta", website_url="https://beta.com"
        )
        response = self.client.get("/api/company-requests/")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data["requests"]), 2)
        self.assertEqual(data["requests"][0]["company_name"], "Beta")

    def test_get_list_only_own_requests(self):
        self.client.force_login(self.user)
        CompanyRequest.objects.create(
            requester=self.user, company_name="Acme", website_url="https://acme.com"
        )
        CompanyRequest.objects.create(
            requester=self.other_user, company_name="Beta", website_url="https://beta.com"
        )
        response = self.client.get("/api/company-requests/")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data["requests"]), 1)
        self.assertEqual(data["requests"][0]["company_name"], "Acme")

    def test_get_detail(self):
        self.client.force_login(self.user)
        req = CompanyRequest.objects.create(
            requester=self.user, company_name="Acme", website_url="https://acme.com"
        )
        response = self.client.get(f"/api/company-requests/{req.pk}/")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["company_name"], "Acme")
        self.assertEqual(data["status"], "pending")

    def test_get_detail_not_found(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/company-requests/99999/")
        self.assertEqual(response.status_code, 404)

    def test_get_detail_other_user_request(self):
        self.client.force_login(self.user)
        req = CompanyRequest.objects.create(
            requester=self.other_user, company_name="Acme", website_url="https://acme.com"
        )
        response = self.client.get(f"/api/company-requests/{req.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_post_requires_auth(self):
        response = self.client.post(
            "/api/company-requests/",
            data=json.dumps({"company_name": "Acme", "website_url": "https://acme.com"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_post_valid(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/company-requests/",
            data=json.dumps({"company_name": "Acme", "website_url": "https://acme.com"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        data = json.loads(response.content)
        self.assertEqual(data["company_name"], "Acme")
        self.assertEqual(data["status"], "pending")
        self.assertTrue(CompanyRequest.objects.filter(pk=data["id"]).exists())

    def test_post_with_careers_and_reason(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/company-requests/",
            data=json.dumps({
                "company_name": "Acme",
                "website_url": "https://acme.com",
                "careers_url": "https://acme.com/careers",
                "reason": "Great company",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)

    def test_post_invalid_fields(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/company-requests/",
            data=json.dumps({"company_name": "", "website_url": "not-a-url"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("field_errors", data)

    def test_post_invalid_json(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/company-requests/",
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_post_non_dict_json(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/company-requests/",
            data=json.dumps(["not", "a", "dict"]),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_post_duplicate_organization(self):
        Organization.objects.create(name="Acme", url="https://acme.com", status=1)
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/company-requests/",
            data=json.dumps({"company_name": "Acme", "website_url": "https://acme.com"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        data = json.loads(response.content)
        self.assertIn("already in the catalog", data["error"])

    def test_post_duplicate_pending_request(self):
        CompanyRequest.objects.create(
            requester=self.other_user, company_name="Acme", website_url="https://acme.com"
        )
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/company-requests/",
            data=json.dumps({"company_name": "Acme", "website_url": "https://acme.com"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        data = json.loads(response.content)
        self.assertIn("duplicate_request", data)

    def test_post_duplicate_by_domain(self):
        CompanyRequest.objects.create(
            requester=self.other_user,
            company_name="Different Name",
            website_url="https://acme.com",
        )
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/company-requests/",
            data=json.dumps({"company_name": "Totally Different", "website_url": "https://www.acme.com"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)

    def test_rate_limit_recovers_from_cache_value_error(self):
        request = RequestFactory().get("/api/company-requests/")
        request.user = self.user
        cache.set(f"company-request-rate:{self.user.pk}", 1)
        with patch.object(cache, "incr", side_effect=ValueError):
            self.assertFalse(_rate_limited(request))
        self.assertEqual(cache.get(f"company-request-rate:{self.user.pk}"), 1)

    def test_post_rate_limited(self):
        self.client.force_login(self.user)
        for _ in range(5):
            self.client.post(
                "/api/company-requests/",
                data=json.dumps({
                    "company_name": f"Corp {_}",
                    "website_url": f"https://corp-{_}.com",
                }),
                content_type="application/json",
            )
        response = self.client.post(
            "/api/company-requests/",
            data=json.dumps({"company_name": "Sixth", "website_url": "https://sixth.com"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 429)

    def test_post_re_raises_unmatched_integrity_error(self):
        self.client.force_login(self.user)
        empty_chain = MagicMock()
        empty_chain.filter.return_value = empty_chain
        empty_chain.first.return_value = None
        with patch("crank.views.company_requests.CompanyRequest.objects.filter", side_effect=[empty_chain, empty_chain, empty_chain, empty_chain]), \
             patch("crank.views.company_requests.CompanyRequest.save", side_effect=IntegrityError):
            with self.assertRaises(IntegrityError):
                self.client.post("/api/company-requests/", data=json.dumps({"company_name": "Unmatched", "website_url": "https://unmatched.example.com"}), content_type="application/json")

    def test_post_handles_concurrent_duplicate(self):
        self.client.force_login(self.user)
        concurrent_request = CompanyRequest.objects.create(requester=self.other_user, company_name="Concurrent", website_url="https://concurrent.example.com")
        empty_chain = MagicMock(); empty_chain.filter.return_value = empty_chain; empty_chain.first.return_value = None
        existing_chain = MagicMock(); existing_chain.filter.return_value = existing_chain; existing_chain.first.return_value = concurrent_request
        with patch("crank.views.company_requests.CompanyRequest.objects.filter", side_effect=[empty_chain, empty_chain, existing_chain]), \
             patch("crank.views.company_requests.CompanyRequest.save", side_effect=IntegrityError):
            response = self.client.post("/api/company-requests/", data=json.dumps({"company_name": "New Concurrent", "website_url": "https://new.example.com"}), content_type="application/json")
        self.assertEqual(response.status_code, 409)
        self.assertIn("duplicate_request", json.loads(response.content))

    def test_post_with_pk_returns_405(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/company-requests/1/",
            data=json.dumps({"company_name": "Acme", "website_url": "https://acme.com"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 405)

    def test_post_empty_company_name_form_error(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/company-requests/",
            data=json.dumps({"company_name": "   ", "website_url": "https://acme.com"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_put_method_not_allowed(self):
        self.client.force_login(self.user)
        response = self.client.put("/api/company-requests/")
        self.assertEqual(response.status_code, 405)

    def test_request_payload_fields(self):
        self.client.force_login(self.user)
        org = Organization.objects.create(name="Existing", url="https://existing.com", status=1)
        req = CompanyRequest.objects.create(
            requester=self.user,
            company_name="Test",
            website_url="https://test.com",
            status=CompanyRequest.Status.DUPLICATE,
            duplicate_of=org,
            admin_note="Moderator note",
        )
        response = self.client.get(f"/api/company-requests/{req.pk}/")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["duplicate_of"]["name"], "Existing")
        self.assertEqual(data["admin_note"], "Moderator note")
        self.assertIn("created", data)
        self.assertIn("modified", data)


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class CompanyRequestAdminTest(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.admin_user = User.objects.create_superuser(
            username="admin", password="adminpass", email="admin@test.com"
        )
        self.client = Client()
        self.client.force_login(self.admin_user)
        self.admin = CompanyRequestAdmin(CompanyRequest, self.site)
        self.user = User.objects.create_user(
            username="requester", password="***", email="req@test.com"
        )

    def tearDown(self):
        cache.clear()

    def _make_request(self, **kwargs):
        defaults = {
            "requester": self.user,
            "company_name": "Acme",
            "website_url": "https://acme.com",
        }
        defaults.update(kwargs)
        return CompanyRequest.objects.create(**defaults)

    def _mock_post(self, confirm="yes"):
        request = self.factory.post("/", {"confirm": confirm})
        request.user = self.admin_user
        request.session = SessionStore()
        setattr(request, '_messages', FallbackStorage(request))
        return request

    def test_approve_without_confirmation(self):
        req = self._make_request()
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.approve_requests(self._mock_post(confirm="no"), qs)
        req.refresh_from_db()
        self.assertEqual(req.status, CompanyRequest.Status.PENDING)
        self.assertFalse(OperationalChangeAudit.objects.exists())

    def test_approve_creates_organization(self):
        req = self._make_request()
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.approve_requests(self._mock_post(), qs)
        req.refresh_from_db()
        self.assertEqual(req.status, CompanyRequest.Status.APPROVED)
        self.assertTrue(req.approved_organization_id)
        org = req.approved_organization
        self.assertEqual(org.name, "Acme")
        self.assertEqual(org.url, "https://acme.com/")
        self.assertEqual(org.status, 0)
        self.assertTrue(OperationalChangeAudit.objects.filter(
            target_type="company_request", action="approve"
        ).exists())

    def test_approve_skips_non_pending(self):
        req = self._make_request(status=CompanyRequest.Status.REJECTED)
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.approve_requests(self._mock_post(), qs)
        req.refresh_from_db()
        self.assertEqual(req.status, CompanyRequest.Status.REJECTED)
        self.assertFalse(req.approved_organization_id)

    def test_reject_without_confirmation(self):
        req = self._make_request()
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.reject_requests(self._mock_post(confirm="no"), qs)
        req.refresh_from_db()
        self.assertEqual(req.status, CompanyRequest.Status.PENDING)

    def test_reject_sets_status(self):
        req = self._make_request()
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.reject_requests(self._mock_post(), qs)
        req.refresh_from_db()
        self.assertEqual(req.status, CompanyRequest.Status.REJECTED)
        self.assertTrue(OperationalChangeAudit.objects.filter(
            action="reject"
        ).exists())

    def test_mark_duplicate_without_confirmation(self):
        org = Organization.objects.create(name="Existing", url="https://existing.com", status=1)
        req = self._make_request(duplicate_of=org)
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.mark_duplicate(self._mock_post(confirm="no"), qs)
        req.refresh_from_db()
        self.assertEqual(req.status, CompanyRequest.Status.PENDING)

    def test_mark_duplicate_sets_status(self):
        org = Organization.objects.create(name="Existing", url="https://existing.com", status=1)
        req = self._make_request(duplicate_of=org)
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.mark_duplicate(self._mock_post(), qs)
        req.refresh_from_db()
        self.assertEqual(req.status, CompanyRequest.Status.DUPLICATE)
        self.assertTrue(OperationalChangeAudit.objects.filter(
            action="duplicate"
        ).exists())

    def test_mark_duplicate_requires_duplicate_of(self):
        req = self._make_request()
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.mark_duplicate(self._mock_post(), qs)
        req.refresh_from_db()
        self.assertEqual(req.status, CompanyRequest.Status.PENDING)

    def test_approve_crawl_source(self):
        req = self._make_request(status=CompanyRequest.Status.APPROVED)
        org = Organization.objects.create(name="Acme", url="https://acme.com", status=1)
        req.approved_organization = org
        req.save()
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.approve_crawl_sources(self._mock_post(), qs)
        req.refresh_from_db()
        self.assertTrue(req.crawl_source_approved)
        self.assertTrue(OperationalChangeAudit.objects.filter(
            action="approve_source"
        ).exists())

    def test_approve_crawl_source_without_confirmation(self):
        req = self._make_request(status=CompanyRequest.Status.APPROVED)
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.approve_crawl_sources(self._mock_post(confirm="no"), qs)
        req.refresh_from_db()
        self.assertFalse(req.crawl_source_approved)

    def test_approve_crawl_source_skips_already_approved(self):
        req = self._make_request(status=CompanyRequest.Status.APPROVED, crawl_source_approved=True)
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.approve_crawl_sources(self._mock_post(), qs)
        req.refresh_from_db()
        self.assertTrue(req.crawl_source_approved)

    def test_queue_refresh(self):
        org = Organization.objects.create(name="Acme", url="https://acme.com", status=1)
        req = self._make_request(status=CompanyRequest.Status.APPROVED,
                                 approved_organization=org,
                                 crawl_source_approved=True)
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.queue_refresh(self._mock_post(), qs)
        req.refresh_from_db()
        self.assertTrue(req.refresh_queued)
        self.assertTrue(OperationalChangeAudit.objects.filter(
            action="queue_refresh"
        ).exists())

    def test_queue_refresh_without_confirmation(self):
        org = Organization.objects.create(name="Acme", url="https://acme.com", status=1)
        req = self._make_request(status=CompanyRequest.Status.APPROVED,
                                 approved_organization=org,
                                 crawl_source_approved=True)
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.queue_refresh(self._mock_post(confirm="no"), qs)
        req.refresh_from_db()
        self.assertFalse(req.refresh_queued)

    def test_queue_refresh_requires_source_approval(self):
        req = self._make_request(status=CompanyRequest.Status.APPROVED, crawl_source_approved=False)
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.queue_refresh(self._mock_post(), qs)
        req.refresh_from_db()
        self.assertFalse(req.refresh_queued)

    def test_queue_refresh_skips_already_queued(self):
        org = Organization.objects.create(name="Acme", url="https://acme.com", status=1)
        req = self._make_request(status=CompanyRequest.Status.APPROVED,
                                 approved_organization=org,
                                 crawl_source_approved=True,
                                 refresh_queued=True)
        qs = CompanyRequest.objects.filter(pk=req.pk)
        self.admin.queue_refresh(self._mock_post(), qs)
        req.refresh_from_db()
        self.assertTrue(req.refresh_queued)

    def test_staff_only_permission(self):
        from crank.admin import StaffOnlyAdminMixin
        non_staff = User.objects.create_user(username="nonstaff", password="***")
        mixin = StaffOnlyAdminMixin()
        self.assertTrue(mixin.has_module_permission(
            type("r", (), {"user": self.admin_user})()
        ))
        self.assertFalse(mixin.has_module_permission(
            type("r", (), {"user": non_staff})()
        ))
