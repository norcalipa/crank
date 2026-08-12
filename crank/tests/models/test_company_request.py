# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from crank.models.company_request import (
    CompanyRequest,
    _unsafe_hostname,
    normalize_company_name,
    normalize_domain,
    normalize_public_url,
)
from crank.models.organization import Organization


class NormalizeCompanyNameTest(TestCase):
    def test_normalizes_whitespace(self):
        self.assertEqual(normalize_company_name("  Foo   Bar  "), "foo bar")

    def test_normalizes_case(self):
        self.assertEqual(normalize_company_name("FOO"), "foo")

    def test_handles_unicode(self):
        self.assertEqual(normalize_company_name("café"), "café")

    def test_empty_string(self):
        self.assertEqual(normalize_company_name(""), "")

    def test_none(self):
        self.assertEqual(normalize_company_name(None), "")


class NormalizeDomainTest(TestCase):
    def test_extracts_hostname(self):
        self.assertEqual(normalize_domain("https://example.com/path"), "example.com")

    def test_removes_www(self):
        self.assertEqual(normalize_domain("https://www.example.com"), "example.com")

    def test_casefold(self):
        self.assertEqual(normalize_domain("https://Example.COM"), "example.com")

    def test_adds_scheme_if_missing(self):
        self.assertEqual(normalize_domain("example.com"), "example.com")

    def test_empty_string(self):
        self.assertEqual(normalize_domain(""), "")


class NormalizePublicUrlTest(TestCase):
    def test_valid_https(self):
        self.assertEqual(normalize_public_url("https://example.com"), "https://example.com/")

    def test_valid_https_with_path(self):
        self.assertEqual(normalize_public_url("https://example.com/path"), "https://example.com/path")

    def test_rejects_http(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("http://example.com")

    def test_rejects_localhost(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://localhost")

    def test_rejects_private_ip(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://192.168.1.1")

    def test_rejects_credentials(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://user:pass@example.com")

    def test_rejects_non_standard_port(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://example.com:8080")

    def test_allows_port_443(self):
        self.assertEqual(normalize_public_url("https://example.com:443"), "https://example.com:443/")

    def test_rejects_fragment(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://example.com#frag")

    def test_empty_required(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("")

    def test_empty_optional(self):
        self.assertEqual(normalize_public_url("", required=False), "")

    def test_rejects_local_domain(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://myapp.local")

    def test_rejects_loopback_ip(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://127.0.0.1")

    def test_rejects_link_local_ip(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://169.254.1.1")

    def test_rejects_reserved_ip(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://0.0.0.0")

    def test_rejects_multicast_ip(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://224.0.0.1")

    def test_rejects_invalid_port(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://example.com:abc")

    def test_rejects_out_of_range_port(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://example.com:99999")

    def test_rejects_localhost_domain(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://sub.localhost")

    def test_rejects_internal_domain(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://app.internal")

    def test_preserves_query_string(self):
        result = normalize_public_url("https://example.com?q=1")
        self.assertEqual(result, "https://example.com/?q=1")

    def test_strips_trailing_dot(self):
        result = normalize_public_url("https://example.com.")
        self.assertEqual(result, "https://example.com/")


class UnsafeHostnameTest(TestCase):
    def test_localhost(self):
        self.assertTrue(_unsafe_hostname("localhost"))

    def test_private_ip(self):
        self.assertTrue(_unsafe_hostname("192.168.1.1"))

    def test_loopback_ip(self):
        self.assertTrue(_unsafe_hostname("127.0.0.1"))

    def test_public_domain(self):
        self.assertFalse(_unsafe_hostname("example.com"))

    def test_local_domain(self):
        self.assertTrue(_unsafe_hostname("myapp.local"))

    def test_internal_domain(self):
        self.assertTrue(_unsafe_hostname("myapp.internal"))

    def test_lan_domain(self):
        self.assertTrue(_unsafe_hostname("myapp.lan"))

    def test_empty(self):
        self.assertTrue(_unsafe_hostname(""))

    def test_localhost_subdomain(self):
        self.assertTrue(_unsafe_hostname("sub.localhost"))


class CompanyRequestFormTest(TestCase):
    def test_blank_company_name_validation_message(self):
        from crank.forms.company_request import CompanyRequestForm
        form = CompanyRequestForm()
        form.cleaned_data = {"company_name": "   "}
        with self.assertRaises(ValidationError):
            form.clean_company_name()


class CompanyRequestModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", password="***", email="test@test.com"
        )

    def test_create_valid_request(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme Corp",
            website_url="https://acme.com",
        )
        req.save()
        self.assertEqual(req.normalized_name, "acme corp")
        self.assertEqual(req.normalized_domain, "acme.com")
        self.assertEqual(req.status, CompanyRequest.Status.PENDING)
        self.assertFalse(req.crawl_source_approved)
        self.assertFalse(req.refresh_queued)

    def test_normalizes_name_and_domain_on_save(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="  ACME Corp  ",
            website_url="https://www.acme.com",
        )
        req.save()
        self.assertEqual(req.normalized_name, "acme corp")
        self.assertEqual(req.normalized_domain, "acme.com")

    def test_rejects_http_website(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="http://acme.com",
        )
        with self.assertRaises(ValidationError):
            req.save()

    def test_rejects_private_ip_website(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="https://192.168.1.1",
        )
        with self.assertRaises(ValidationError):
            req.save()

    def test_rejects_localhost_website(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="https://localhost:8000",
        )
        with self.assertRaises(ValidationError):
            req.save()

    def test_careers_url_optional(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="https://acme.com",
            careers_url="",
        )
        req.save()
        self.assertEqual(req.careers_url, "")

    def test_valid_careers_url(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="https://acme.com",
            careers_url="https://acme.com/careers",
        )
        req.save()
        self.assertEqual(req.careers_url, "https://acme.com/careers")

    def test_rejects_http_careers_url(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="https://acme.com",
            careers_url="http://acme.com/careers",
        )
        with self.assertRaises(ValidationError):
            req.save()

    def test_duplicate_status_requires_duplicate_of(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="https://acme.com",
            status=CompanyRequest.Status.DUPLICATE,
        )
        with self.assertRaises(ValidationError):
            req.save()

    def test_reason_stored(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="https://acme.com",
            reason="Great company",
        )
        req.save()
        self.assertEqual(req.reason, "Great company")

    def test_str_representation(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="https://acme.com",
        )
        req.save()
        self.assertEqual(str(req), "Acme (pending)")

    def test_find_existing_organization_by_name(self):
        org = Organization.objects.create(
            name="Acme Corp", url="https://acme.com", status=1
        )
        found = CompanyRequest.find_existing_organization(
            normalized_name="acme corp", normalized_domain="acme.com"
        )
        self.assertEqual(found, org)

    def test_find_existing_organization_by_domain(self):
        org = Organization.objects.create(
            name="Different Name", url="https://acme.com", status=1
        )
        found = CompanyRequest.find_existing_organization(
            normalized_name="no match", normalized_domain="acme.com"
        )
        self.assertEqual(found, org)

    def test_find_existing_organization_none(self):
        found = CompanyRequest.find_existing_organization(
            normalized_name="no match", normalized_domain="nomatch.com"
        )
        self.assertIsNone(found)

    def test_find_existing_organization_skips_inactive(self):
        Organization.objects.create(
            name="Acme Corp", url="https://acme.com", status=0
        )
        found = CompanyRequest.find_existing_organization(
            normalized_name="acme corp", normalized_domain="acme.com"
        )
        self.assertIsNone(found)

    def test_non_pending_allows_same_name(self):
        CompanyRequest.objects.create(
            requester=self.user, company_name="Acme", website_url="https://acme.com",
            status=CompanyRequest.Status.REJECTED,
        )
        req = CompanyRequest(
            requester=self.user, company_name="Acme", website_url="https://acme.com"
        )
        req.save()
        self.assertEqual(req.status, CompanyRequest.Status.PENDING)

    def test_rejects_invalid_port(self):
        with self.assertRaises(ValidationError):
            normalize_public_url("https://example.com:abc")

    def test_rejects_reason_too_long(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="https://acme.com",
            reason="x" * 501,
        )
        with self.assertRaises(ValidationError):
            req.save()

    def test_rejects_admin_note_too_long(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="https://acme.com",
            admin_note="x" * 501,
        )
        with self.assertRaises(ValidationError):
            req.save()

    def test_admin_note_stored(self):
        req = CompanyRequest(
            requester=self.user,
            company_name="Acme",
            website_url="https://acme.com",
            admin_note="Reviewed by staff",
        )
        req.save()
        self.assertEqual(req.admin_note, "Reviewed by staff")
