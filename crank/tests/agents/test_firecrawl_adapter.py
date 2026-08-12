# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Offline tests for the policy-gated Firecrawl careers adapter."""

from __future__ import annotations

import json
import pytest
from django.test import TestCase, override_settings
import requests

from crank.agents.jobs.base import JobSourceQuery
from crank.agents.jobs.errors import JobSourceDisabled, SourceBudgetExceededError
from crank.agents.jobs.firecrawl import (
    EXTRACTION_SCHEMA,
    EXTRACTION_VERSION,
    FirecrawlCareersAdapter,
    FirecrawlClient,
    _compensation,
    _remote,
    _text,
)
from crank.agents.jobs.ingest import ingest_jobs
from crank.agents.sources import errors as source_errors
from crank.models.job import JobListing, JobSourceCatalog


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response or {"id": "crawl-1", "status": "completed", "data": []}
        self.error = error
        self.calls = []

    def crawl_url(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class Response:
    def __init__(self, body, status_code=200):
        self.content = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}


def source(**changes):
    values = {
        "name": "Example Careers",
        "adapter_key": "firecrawl-careers",
        "base_url": "https://jobs.example.test/careers",
        "approval_state": JobSourceCatalog.ApprovalState.APPROVED,
        "enabled": True,
    }
    values.update(changes)
    return JobSourceCatalog.objects.create(**values)


def extracted(**changes):
    values = {
        "title": "Senior API Engineer",
        "canonical_url": "https://jobs.example.test/jobs/123",
        "employer": "Example Labs",
        "location": "Remote",
        "remote_status": True,
        "compensation": {"min": 100000, "max": 125000, "currency": "USD", "interval": "year"},
        "description_excerpt": "Build safe APIs.",
        "source_id": "job-123",
        "status": "active",
    }
    values.update(changes)
    return values


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


class HelperFunctionTests(TestCase):
    def test_text_rejects_non_string(self):
        with pytest.raises(source_errors.SchemaDriftError):
            _text(123, "field")

    def test_text_rejects_empty_required(self):
        with pytest.raises(source_errors.SchemaDriftError):
            _text("   ", "field", required=True)

    def test_text_rejects_too_long(self):
        with pytest.raises(source_errors.SchemaDriftError):
            _text("x" * 10, "field", maximum=5)

    def test_remote_string_value(self):
        assert _remote("remote") is True
        assert _remote("fully remote") is True
        assert _remote("true") is True
        assert _remote("yes") is True
        assert _remote("onsite") is False

    def test_remote_none_value(self):
        assert _remote(None) is False

    def test_remote_invalid_type(self):
        with pytest.raises(source_errors.SchemaDriftError):
            _remote(123)

    def test_compensation_none(self):
        assert _compensation(None) == {}

    def test_compensation_empty_string(self):
        assert _compensation("") == {}

    def test_compensation_freeform_string(self):
        assert _compensation("100k-120k") == {}

    def test_compensation_non_mapping(self):
        with pytest.raises(source_errors.SchemaDriftError):
            _compensation([1, 2])

    def test_compensation_object_with_alternate_keys(self):
        result = _compensation({"minimum": 50000, "maximum": 60000, "currency_code": "EUR", "rate": "hour"})
        assert result["compensation_min"] == 50000
        assert result["compensation_max"] == 60000
        assert result["compensation_currency"] == "EUR"
        assert result["compensation_interval"] == "hour"

    def test_compensation_object_with_direct_keys(self):
        result = _compensation({"compensation_min": 70000, "compensation_max": 80000})
        assert result["compensation_min"] == 70000
        assert result["compensation_max"] == 80000


# ---------------------------------------------------------------------------
# Adapter tests
# ---------------------------------------------------------------------------


@override_settings(
    FIRECRAWL_ENABLED=True,
    FIRECRAWL_API_KEY="fixture-secret",
    FIRECRAWL_MAX_PAGES=3,
    FIRECRAWL_MAX_LISTINGS=5,
    FIRECRAWL_CREDIT_BUDGET=5,
)
class FirecrawlAdapterTests(TestCase):
    def make_adapter(self, response=None, client=None, **source_changes):
        return FirecrawlCareersAdapter(
            source(**source_changes),
            client=client or FakeClient(response=response),
        )

    @override_settings(FIRECRAWL_ENABLED=False, FIRECRAWL_API_KEY="")
    def test_disabled_default_fails_closed_before_external_call(self):
        with pytest.raises(JobSourceDisabled):
            FirecrawlCareersAdapter(source(), client=FakeClient())

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="")
    def test_missing_credentials_fail_closed(self):
        with pytest.raises(source_errors.UnauthorizedSourceError):
            FirecrawlCareersAdapter(source(), client=FakeClient())

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_normalizes_versioned_schema_and_bounded_provenance(self):
        client = FakeClient({"id": "crawl-1", "status": "completed", "data": [
            {"extract": extracted(), "metadata": {"sourceURL": "https://jobs.example.test/careers"}}
        ]})
        adapter = self.make_adapter(client=client)
        result = adapter.fetch(JobSourceQuery(max_pages=10, max_listings=10))
        listing = result.listings[0]
        assert listing.external_id == "job-123"
        assert listing.canonical_url == "https://jobs.example.test/jobs/123"
        assert listing.employer_name == "Example Labs"
        assert listing.is_remote is True
        assert listing.compensation_min == 100000
        assert listing.compensation_max == 125000
        assert listing.description_excerpt == "Build safe APIs."
        assert listing.source_metadata["extraction_version"] == EXTRACTION_VERSION
        assert listing.source_metadata["crawl_job_id"] == "crawl-1"
        assert set(listing.source_metadata) == {
            "source_url", "crawl_job_id", "extraction_version", "observed_at"
        }
        assert "html" not in repr(listing.source_metadata).lower()
        assert client.calls[0][1]["max_pages"] == 3
        assert client.calls[0][1]["max_listings"] == 5
        assert client.calls[0][1]["extraction_schema"] == EXTRACTION_SCHEMA

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_off_domain_listing_is_rejected(self):
        client = FakeClient({"id": "crawl-1", "data": [{"extract": extracted(
            canonical_url="https://evil.example/jobs/123"
        )}]})
        with pytest.raises(source_errors.BlockedRedirectError):
            self.make_adapter(client=client).fetch(JobSourceQuery())

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_malformed_extraction_is_rejected_without_payload_in_error(self):
        client = FakeClient({"id": "crawl-1", "data": [{"extract": {"title": []}}]})
        with pytest.raises(source_errors.SchemaDriftError) as exc:
            self.make_adapter(client=client).fetch(JobSourceQuery())
        assert "<html>" not in str(exc.value)

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret", FIRECRAWL_CREDIT_BUDGET=0)
    def test_exhausted_budget_makes_no_client_call(self):
        client = FakeClient()
        with pytest.raises(SourceBudgetExceededError):
            self.make_adapter(client=client).fetch(JobSourceQuery())
        assert client.calls == []

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_source_domain_must_be_code_approved(self):
        source_obj = source()
        source_obj.base_url = "https://operator-added.example/careers"
        with pytest.raises(source_errors.BlockedRedirectError):
            FirecrawlCareersAdapter(source_obj, client=FakeClient())

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_source_with_wrong_adapter_key_is_rejected(self):
        source_obj = source()
        source_obj.adapter_key = "wrong-adapter"
        with pytest.raises(source_errors.BlockedRedirectError):
            FirecrawlCareersAdapter(source_obj, client=FakeClient())

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_data_not_list_raises_schema_drift(self):
        client = FakeClient({"id": "crawl-1", "data": "not-a-list"})
        with pytest.raises(source_errors.SchemaDriftError):
            self.make_adapter(client=client).fetch(JobSourceQuery())

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_non_mapping_listing_item_raises(self):
        client = FakeClient({"id": "crawl-1", "data": ["not-a-mapping"]})
        with pytest.raises(source_errors.SchemaDriftError):
            self.make_adapter(client=client).fetch(JobSourceQuery())

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_non_mapping_extraction_raises(self):
        client = FakeClient({"id": "crawl-1", "data": [{"extract": "not-a-mapping"}]})
        with pytest.raises(source_errors.SchemaDriftError):
            self.make_adapter(client=client).fetch(JobSourceQuery())

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_non_mapping_metadata_raises(self):
        client = FakeClient({"id": "crawl-1", "data": [{"extract": extracted(), "metadata": "not-a-mapping"}]})
        with pytest.raises(source_errors.SchemaDriftError):
            self.make_adapter(client=client).fetch(JobSourceQuery())

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_none_metadata_treated_as_empty(self):
        client = FakeClient({"id": "crawl-1", "data": [{"extract": extracted(), "metadata": None}]})
        result = self.make_adapter(client=client).fetch(JobSourceQuery())
        assert len(result.listings) == 1

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_invalid_status_raises(self):
        client = FakeClient({"id": "crawl-1", "data": [{"extract": extracted(status="deleted")}]})
        with pytest.raises(source_errors.SchemaDriftError):
            self.make_adapter(client=client).fetch(JobSourceQuery())

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_compensation_from_top_level_fields(self):
        client = FakeClient({"id": "crawl-1", "data": [{"extract": extracted(
            compensation=None,
            compensation_min=90000,
            compensation_max=110000,
            compensation_currency="GBP",
            compensation_interval="year",
        )}]})
        result = self.make_adapter(client=client).fetch(JobSourceQuery())
        listing = result.listings[0]
        assert listing.compensation_min == 90000
        assert listing.compensation_max == 110000
        assert listing.compensation_currency == "GBP"
        assert listing.compensation_interval == "year"

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_remote_status_from_string(self):
        client = FakeClient({"id": "crawl-1", "data": [{"extract": extracted(remote_status="remote")}]})
        result = self.make_adapter(client=client).fetch(JobSourceQuery())
        assert result.listings[0].is_remote is True

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_generic_exception_wrapped_as_schema_drift(self):
        class ExplodingClient:
            def crawl_url(self, url, **kwargs):
                raise RuntimeError("unexpected internal error")
        with pytest.raises(source_errors.SchemaDriftError):
            self.make_adapter(client=ExplodingClient()).fetch(JobSourceQuery())

    @override_settings(FIRECRAWL_ENABLED=True, FIRECRAWL_API_KEY="fixture-secret")
    def test_partial_results_do_not_close_unseen_listing_and_ingest_is_idempotent(self):
        first = self.make_adapter(response={"id": "crawl-1", "status": "completed", "data": [
            {"extract": extracted()},
            {"extract": extracted(source_id="job-456", canonical_url="https://jobs.example.test/jobs/456", title="Platform Engineer")},
        ]})
        source_obj = first.source
        initial = ingest_jobs(source_obj, JobSourceQuery(), adapter=first)
        assert initial.ingested == 2
        original = JobListing.all_objects.get(source=source_obj, external_id="job-456")
        partial = FirecrawlCareersAdapter(
            source_obj,
            client=FakeClient({"id": "crawl-2", "status": "partial", "data": [{"extract": extracted()}]}),
        )
        result = ingest_jobs(source_obj, JobSourceQuery(), adapter=partial)
        assert result.errors == 0
        original.refresh_from_db()
        assert original.status == JobListing.Status.ACTIVE
        assert JobListing.all_objects.filter(source=source_obj).count() == 2

        replay = ingest_jobs(source_obj, JobSourceQuery(), adapter=first)
        assert replay.ingested == 0
        assert JobListing.all_objects.filter(source=source_obj, external_id="job-123").count() == 1

    @override_settings(
        FIRECRAWL_ENABLED=True,
        FIRECRAWL_API_KEY="fixture-secret",
        FIRECRAWL_BASE_URL="https://api.firecrawl.dev",
        FIRECRAWL_TIMEOUT=5.0,
    )
    def test_default_client_built_when_none_injected(self):
        adapter = FirecrawlCareersAdapter(source())
        assert adapter.client.base_url == "https://api.firecrawl.dev"
        assert adapter.client.timeout == 5.0


# ---------------------------------------------------------------------------
# FirecrawlClient tests
# ---------------------------------------------------------------------------


class FirecrawlClientTests(TestCase):
    def build_client(self, request, **kwargs):
        return FirecrawlClient(
            base_url="https://api.firecrawl.dev",
            api_key="fixture-secret",
            timeout=1,
            request=request,
            sleep=lambda _: None,
            **kwargs,
        )

    def test_rejects_non_https_base_url(self):
        with pytest.raises(source_errors.BlockedRedirectError):
            FirecrawlClient(base_url="http://api.firecrawl.dev", api_key="k", timeout=1)

    def test_rejects_empty_api_key(self):
        with pytest.raises(source_errors.UnauthorizedSourceError):
            FirecrawlClient(base_url="https://api.firecrawl.dev", api_key="  ", timeout=1)

    def test_rejects_base_url_with_credentials(self):
        with pytest.raises(source_errors.BlockedRedirectError):
            FirecrawlClient(base_url="https://user:pass@api.firecrawl.dev", api_key="k", timeout=1)

    def test_async_crawl_polls_with_bounded_requests(self):
        responses = iter([
            Response({"id": "crawl-1", "status": "scraping"}),
            Response({"id": "crawl-1", "status": "completed", "data": []}),
        ])
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return next(responses)

        result = self.build_client(request).crawl_url(
            "https://jobs.example.test/careers",
            max_pages=2,
            max_listings=3,
            credit_budget=4,
            extraction_schema=EXTRACTION_SCHEMA,
        )
        assert result["status"] == "completed"
        assert [call[0] for call in calls] == ["POST", "GET"]
        assert calls[0][2]["headers"]["Authorization"] == "Bearer fixture-secret"
        assert "fixture-secret" not in repr(result)

    def test_timeout_and_malformed_response_are_typed_and_sanitized(self):
        def timeout(*args, **kwargs):
            raise requests.Timeout("secret response body")

        with pytest.raises(source_errors.SourceTimeoutError) as exc:
            self.build_client(timeout).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )
        assert "secret" not in str(exc.value)

        with pytest.raises(source_errors.SchemaDriftError):
            self.build_client(lambda *args, **kwargs: Response(b"not-json")).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_credentials_are_only_request_headers_and_not_persisted(self):
        calls = []

        def request(method, url, **kwargs):
            calls.append(kwargs)
            return Response({"id": "crawl-1", "status": "completed", "data": []})

        self.build_client(request).crawl_url(
            "https://jobs.example.test/careers", max_pages=1, max_listings=1,
            credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
        )
        assert calls[0]["headers"]["Authorization"] == "Bearer fixture-secret"
        assert "fixture-secret" not in repr(Response({"id": "crawl-1", "data": []}))

    def test_redirect_response_raises(self):
        with pytest.raises(source_errors.BlockedRedirectError):
            self.build_client(lambda *a, **kw: Response({}, status_code=302)).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_unauthorized_response_raises(self):
        with pytest.raises(source_errors.UnauthorizedSourceError):
            self.build_client(lambda *a, **kw: Response({}, status_code=403)).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_server_error_response_raises(self):
        with pytest.raises(source_errors.SourceServerError):
            self.build_client(lambda *a, **kw: Response({}, status_code=500)).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_throttled_response_raises_server_error(self):
        with pytest.raises(source_errors.SourceServerError):
            self.build_client(lambda *a, **kw: Response({}, status_code=429)).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_unexpected_status_raises_schema_drift(self):
        with pytest.raises(source_errors.SchemaDriftError):
            self.build_client(lambda *a, **kw: Response({}, status_code=422)).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_oversized_response_raises(self):
        big = b'{"x": "' + b"A" * (2 * 1024 * 1024 + 100) + b'"}'
        with pytest.raises(source_errors.SchemaDriftError):
            self.build_client(lambda *a, **kw: Response(big)).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_non_object_json_raises(self):
        with pytest.raises(source_errors.SchemaDriftError):
            self.build_client(lambda *a, **kw: Response(b"[1,2,3]")).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_credit_budget_zero_raises(self):
        with pytest.raises(SourceBudgetExceededError):
            self.build_client(lambda *a, **kw: Response({})).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=0, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_missing_job_id_raises(self):
        with pytest.raises(source_errors.SchemaDriftError):
            self.build_client(lambda *a, **kw: Response({"status": "scraping"})).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_failed_crawl_status_raises(self):
        responses = iter([
            Response({"id": "crawl-1", "status": "scraping"}),
            Response({"id": "crawl-1", "status": "failed"}),
        ])
        with pytest.raises(source_errors.SourceServerError):
            self.build_client(lambda *a, **kw: next(responses)).crawl_url(
                "https://jobs.example.test/careers", max_pages=2, max_listings=2,
                credit_budget=2, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_polling_timeout_raises(self):
        # Always returns "scraping" — never completes
        with pytest.raises(source_errors.SourceTimeoutError):
            self.build_client(lambda *a, **kw: Response({"id": "crawl-1", "status": "scraping"})).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_sync_crawl_returns_data_directly(self):
        # When the POST response already contains "data", no polling needed
        result = self.build_client(lambda *a, **kw: Response({"data": [], "status": "completed"})).crawl_url(
            "https://jobs.example.test/careers", max_pages=1, max_listings=1,
            credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
        )
        assert result["data"] == []

    def test_request_exception_raises_server_error(self):
        def bad_request(*args, **kwargs):
            raise requests.RequestException("network failure")
        with pytest.raises(source_errors.SourceServerError):
            self.build_client(bad_request).crawl_url(
                "https://jobs.example.test/careers", max_pages=1, max_listings=1,
                credit_budget=1, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_cancelled_crawl_without_data_raises(self):
        responses = iter([
            Response({"id": "crawl-1", "status": "scraping"}),
            Response({"id": "crawl-1", "status": "cancelled"}),
        ])
        with pytest.raises(source_errors.SourceServerError):
            self.build_client(lambda *a, **kw: next(responses)).crawl_url(
                "https://jobs.example.test/careers", max_pages=2, max_listings=2,
                credit_budget=2, extraction_schema=EXTRACTION_SCHEMA,
            )

    def test_cancelled_crawl_with_data_returns(self):
        responses = iter([
            Response({"id": "crawl-1", "status": "scraping"}),
            Response({"id": "crawl-1", "status": "cancelled", "data": [{"x": 1}]}),
        ])
        result = self.build_client(lambda *a, **kw: next(responses)).crawl_url(
            "https://jobs.example.test/careers", max_pages=2, max_listings=2,
            credit_budget=2, extraction_schema=EXTRACTION_SCHEMA,
        )
        assert result["status"] == "cancelled"
