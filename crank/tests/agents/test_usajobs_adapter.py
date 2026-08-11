# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Offline tests for the USAJOBS adapter vertical slice."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.test import TestCase, override_settings
import requests

from crank.agents.jobs.base import JobSourceQuery
from crank.agents.jobs.usajobs import USAJobsAdapter
from crank.agents.sources import errors
from crank.agents.sources.transport import SafeHTTPClient
from crank.models.job import JobSourceCatalog


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jobs"


class Response:
    def __init__(self, status_code=200, body=b"", headers=None):
        self.status_code = status_code
        self.content = body
        self.headers = dict(headers or {"Content-Type": "application/json"})

    def iter_content(self, chunk_size=65536):
        yield self.content


def response(name, status_code=200):
    return Response(status_code, (FIXTURES / name).read_bytes())


def json_response(data, status_code=200):
    return Response(status_code, json.dumps(data).encode("utf-8"))


def http_for(responses, *, resolver=lambda host: ["8.8.8.8"], attempts=1):
    calls = []
    values = list(responses)

    def request(method, url, **kwargs):
        calls.append((url, kwargs.get("params"), kwargs.get("headers", {})))
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value

    return SafeHTTPClient(
        allowed_hosts=["data.usajobs.gov"],
        expected_content_type="application/json",
        use_requests=request,
        resolver=resolver,
        max_transient_attempts=attempts,
        backoff_base=0,
        auth_headers={"Authorization-Key": "fixture-key", "User-Agent": "fixture@example.test"},
    ), calls


def source():
    return JobSourceCatalog.objects.create(
        name="USAJOBS fixtures",
        adapter_key="usajobs",
        base_url="https://data.usajobs.gov",
        approval_state=JobSourceCatalog.ApprovalState.APPROVED,
        enabled=True,
    )


def make_entry(
    *,
    object_id="fixture-1001",
    title="Senior API Engineer",
    org="Fixture Labs",
    uri="https://www.usajobs.gov/job/fixture-1001",
    location="Remote",
    remote=True,
    remuneration=None,
    description="Build safe APIs.",
    end_date="2099-12-31",
    status=None,
    user_area=None,
):
    descriptor = {
        "PositionTitle": title,
        "OrganizationName": org,
        "PositionURI": uri,
        "PositionLocationDisplay": location,
        "RemoteIndicator": remote,
        "PositionEndDate": end_date,
    }
    if remuneration is not None:
        descriptor["PositionRemuneration"] = remuneration
    else:
        descriptor["PositionRemuneration"] = [
            {
                "MinimumRange": "100000",
                "MaximumRange": "125000",
                "RateIntervalCode": "PA",
                "CurrencyCode": "USD",
            }
        ]
    if description is not None:
        descriptor["PositionFormattedDescription"] = description
    if status is not None:
        descriptor["PositionStatus"] = status
    if user_area is not None:
        descriptor["UserArea"] = user_area
    return {"MatchedObjectId": object_id, "MatchedObjectDescriptor": descriptor}


def payload(entries, count=None):
    if count is None:
        count = len(entries)
    return {"SearchResult": {"SearchResultCount": count, "SearchResultItems": entries}}


class USAJobsAdapterTests(TestCase):
    def adapter(self, http, source_obj=None):
        return USAJobsAdapter(
            source_obj or source(),
            http=http,
            auth_key="fixture-key",
            user_agent_email="fixture@example.test",
        )

    # ------------------------------------------------------------------
    # Happy path: field mapping and HTML sanitisation
    # ------------------------------------------------------------------

    def test_first_page_maps_fields_and_untrusted_html(self):
        source_obj = source()
        http, calls = http_for([response("usajobs_page1.json")])
        result = self.adapter(http, source_obj).fetch(
            JobSourceQuery(keyword="engineer", location="Remote", max_pages=1)
        )
        listing = result.listings[0]
        assert result.pages_fetched == 1
        assert result.items_seen == 1
        assert listing.external_id == "fixture-1001"
        assert listing.title == "Senior API Engineer"
        assert listing.compensation_min == 100000
        assert listing.compensation_interval == "PA"
        assert listing.description_excerpt == "Build safe APIs."
        assert calls[0][1]["Keyword"] == "engineer"
        assert calls[0][1]["LocationName"] == "Remote"

    # ------------------------------------------------------------------
    # Pagination and empty response
    # ------------------------------------------------------------------

    def test_pagination_and_empty_response(self):
        source_obj = source()
        http, calls = http_for(
            [response("usajobs_page1.json"), response("usajobs_page2.json")]
        )
        result = self.adapter(http, source_obj).fetch(
            JobSourceQuery(max_listings=2, max_pages=2)
        )
        assert [item.external_id for item in result.listings] == [
            "fixture-1001",
            "fixture-1002",
        ]
        assert result.pages_fetched == 2
        assert calls[1][1]["offset"] == 2
        empty_http, _ = http_for([response("usajobs_empty.json")])
        empty = self.adapter(empty_http, source_obj).fetch(JobSourceQuery())
        assert empty.listings == ()

    # ------------------------------------------------------------------
    # Status, dates, and schema failures
    # ------------------------------------------------------------------

    def test_status_dates_and_schema_failures(self):
        source_obj = source()
        http, _ = http_for([response("usajobs_closed_expired.json")])
        result = self.adapter(http, source_obj).fetch(JobSourceQuery(max_listings=2))
        assert [item.status for item in result.listings] == ["closed", "expired"]
        malformed, _ = http_for([response("usajobs_malformed.json")])
        with pytest.raises(errors.MalformedPayloadError):
            self.adapter(malformed, source_obj).fetch(JobSourceQuery())
        drift, _ = http_for([response("usajobs_schema_drift.json")])
        with pytest.raises(errors.SchemaDriftError):
            self.adapter(drift, source_obj).fetch(JobSourceQuery())

    # ------------------------------------------------------------------
    # Transport typed failures and redirect
    # ------------------------------------------------------------------

    def test_transport_typed_failures_and_redirect(self):
        source_obj = source()
        for status, expected in (
            (401, errors.UnauthorizedSourceError),
            (403, errors.UnauthorizedSourceError),
        ):
            http, _ = http_for([Response(status_code=status)], attempts=1)
            with pytest.raises(expected):
                self.adapter(http, source_obj).fetch(JobSourceQuery())
        throttled, _ = http_for(
            [Response(status_code=429, headers={"Retry-After": "2"})], attempts=1
        )
        with pytest.raises(errors.RetriesExhaustedError) as exc:
            self.adapter(throttled, source_obj).fetch(JobSourceQuery())
        assert exc.value.last_error.retry_after == 2
        server, _ = http_for([Response(status_code=503)], attempts=1)
        with pytest.raises(errors.RetriesExhaustedError) as exc:
            self.adapter(server, source_obj).fetch(JobSourceQuery())
        assert isinstance(exc.value.last_error, errors.SourceServerError)
        timeout, _ = http_for(
            [requests.Timeout("fixture timeout")], attempts=1
        )
        with pytest.raises(errors.RetriesExhaustedError) as exc:
            self.adapter(timeout, source_obj).fetch(JobSourceQuery())
        assert isinstance(exc.value.last_error, errors.SourceTimeoutError)
        redirect, _ = http_for(
            [Response(302, headers={"Location": "https://evil.example.test/"})]
        )
        with pytest.raises(errors.BlockedRedirectError):
            self.adapter(redirect, source_obj).fetch(JobSourceQuery())

    # ------------------------------------------------------------------
    # Fail-closed credentials
    # ------------------------------------------------------------------

    def test_missing_credentials_fails_closed(self):
        with pytest.raises(errors.UnauthorizedSourceError):
            USAJobsAdapter(source(), auth_key="", user_agent_email="")

    # ------------------------------------------------------------------
    # _setting_or_env fallback (lines 39-45)
    # ------------------------------------------------------------------

    @override_settings(USAJOBS_AUTH_KEY="from-settings")
    def test_setting_or_env_reads_from_django_settings(self):
        """_setting_or_env prefers Django settings over env vars."""
        from crank.agents.jobs.usajobs import _setting_or_env

        result = _setting_or_env("USAJOBS_AUTH_KEY", "USAJOBS_AUTH_KEY", "")
        assert result == "from-settings"

    def test_setting_or_env_falls_back_to_env(self):
        """_setting_or_env falls back to env when settings attr is missing."""
        import os

        from crank.agents.jobs.usajobs import _setting_or_env

        os.environ["TEST_FALLBACK_KEY"] = "env-value"
        try:
            result = _setting_or_env("NONEXISTENT_SETTING", "TEST_FALLBACK_KEY", "")
            assert result == "env-value"
        finally:
            os.environ.pop("TEST_FALLBACK_KEY", None)

    def test_setting_or_env_returns_default(self):
        """_setting_or_env returns default when neither settings nor env have it."""
        from crank.agents.jobs.usajobs import _setting_or_env

        result = _setting_or_env("NONEXISTENT_SETTING", "DEFINITELY_MISSING_ENV_VAR_XYZ", "fallback")
        assert result == "fallback"

    # ------------------------------------------------------------------
    # _text validation (lines 50, 53)
    # ------------------------------------------------------------------

    def test_text_rejects_non_string(self):
        from crank.agents.jobs.usajobs import _text

        with pytest.raises(errors.SchemaDriftError, match="must be a string"):
            _text(42, "test_field")

    def test_text_rejects_empty_required(self):
        from crank.agents.jobs.usajobs import _text

        with pytest.raises(errors.SchemaDriftError, match="must be non-empty"):
            _text("  ", "test_field", required=True)

    # ------------------------------------------------------------------
    # _number validation (lines 67, 70-71, 73)
    # ------------------------------------------------------------------

    def test_number_rejects_bool(self):
        from crank.agents.jobs.usajobs import _number

        with pytest.raises(errors.SchemaDriftError, match="must be numeric"):
            _number(True, "pay")

    def test_number_rejects_non_numeric_string(self):
        from crank.agents.jobs.usajobs import _number

        with pytest.raises(errors.SchemaDriftError, match="must be numeric"):
            _number("not-a-number", "pay")

    def test_number_rejects_negative(self):
        from crank.agents.jobs.usajobs import _number

        with pytest.raises(errors.SchemaDriftError, match="finite and non-negative"):
            _number(-1, "pay")

    def test_number_returns_none_for_empty(self):
        from crank.agents.jobs.usajobs import _number

        assert _number(None, "pay") is None
        assert _number("", "pay") is None

    # ------------------------------------------------------------------
    # _date validation (lines 79, 83-84)
    # ------------------------------------------------------------------

    def test_date_returns_none_for_empty(self):
        from crank.agents.jobs.usajobs import _date

        assert _date(None, "end") is None
        assert _date("", "end") is None

    def test_date_rejects_invalid_format(self):
        from crank.agents.jobs.usajobs import _date

        with pytest.raises(errors.SchemaDriftError, match="not an ISO date"):
            _date("not-a-date", "end")

    def test_date_accepts_naive_and_adds_utc(self):
        from datetime import datetime, timezone as dt_timezone

        from crank.agents.jobs.usajobs import _date

        result = _date("2025-01-15", "end")
        assert result is not None
        assert result.tzinfo is not None
        assert result.utcoffset() == dt_timezone.utc.utcoffset(None)

    # ------------------------------------------------------------------
    # _nested helper (lines 91-96)
    # ------------------------------------------------------------------

    def test_nested_traverses_path(self):
        from crank.agents.jobs.usajobs import _nested

        data = {"a": {"b": {"c": 42}}}
        assert _nested(data, "a", "b", "c") == 42

    def test_nested_rejects_non_mapping(self):
        from crank.agents.jobs.usajobs import _nested

        with pytest.raises(errors.SchemaDriftError, match="must be an object"):
            _nested({"a": 42}, "a", "b")

    # ------------------------------------------------------------------
    # Wrong API host (line 126)
    # ------------------------------------------------------------------

    def test_wrong_api_host_rejected(self):
        wrong = JobSourceCatalog.objects.create(
            name="Wrong host",
            adapter_key="usajobs",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )
        with pytest.raises(errors.BlockedRedirectError, match="not approved"):
            USAJobsAdapter(
                wrong,
                http=http_for([response("usajobs_page1.json")])[0],
                auth_key="fixture-key",
                user_agent_email="fixture@example.test",
            )

    # ------------------------------------------------------------------
    # Default SafeHTTPClient construction (lines 131-133)
    # ------------------------------------------------------------------

    @override_settings(
        USAJOBS_AUTH_KEY="env-key",
        USAJOBS_USER_AGENT_EMAIL="env@example.test",
        JOBS_ADAPTER_TIMEOUT=5.0,
        JOBS_ADAPTER_MAX_BYTES=1024,
    )
    def test_adapter_constructs_own_http_client(self):
        adapter = USAJobsAdapter(source())
        assert adapter._http.max_bytes == 1024
        assert "data.usajobs.gov" in adapter._http.allowed_hosts

    # ------------------------------------------------------------------
    # max_listings break inside fetch loop (line 165)
    # ------------------------------------------------------------------

    def test_max_listings_cap_stops_pagination(self):
        source_obj = source()
        entries = [make_entry(object_id=f"cap-{i}") for i in range(5)]
        http, _ = http_for([json_response(payload(entries, count=10))])
        result = self.adapter(http, source_obj).fetch(
            JobSourceQuery(max_listings=3, max_pages=10)
        )
        assert len(result.listings) == 3

    # ------------------------------------------------------------------
    # _payload validation (line 179)
    # ------------------------------------------------------------------

    def test_top_level_payload_must_be_object(self):
        source_obj = source()
        http, _ = http_for([json_response([1, 2, 3])])
        with pytest.raises(errors.SchemaDriftError, match="top-level payload must be an object"):
            self.adapter(http, source_obj).fetch(JobSourceQuery())

    # ------------------------------------------------------------------
    # _entries validation (lines 186, 189, 191, 194)
    # ------------------------------------------------------------------

    def test_missing_search_result(self):
        source_obj = source()
        http, _ = http_for([json_response({"OtherKey": {}})])
        with pytest.raises(errors.SchemaDriftError, match="missing SearchResult"):
            self.adapter(http, source_obj).fetch(JobSourceQuery())

    def test_search_result_items_not_a_list(self):
        source_obj = source()
        http, _ = http_for(
            [json_response({"SearchResult": {"SearchResultItems": "not-a-list"}})]
        )
        with pytest.raises(errors.SchemaDriftError, match="SearchResultItems must be a list"):
            self.adapter(http, source_obj).fetch(JobSourceQuery())

    def test_search_result_items_contains_non_objects(self):
        source_obj = source()
        http, _ = http_for(
            [json_response({"SearchResult": {"SearchResultItems": [42]}})]
        )
        with pytest.raises(errors.SchemaDriftError, match="must contain objects"):
            self.adapter(http, source_obj).fetch(JobSourceQuery())

    def test_search_result_count_invalid(self):
        source_obj = source()
        http, _ = http_for(
            [
                json_response(
                    {"SearchResult": {"SearchResultCount": -1, "SearchResultItems": []}}
                )
            ]
        )
        with pytest.raises(errors.SchemaDriftError, match="non-negative integer"):
            self.adapter(http, source_obj).fetch(JobSourceQuery())

    # ------------------------------------------------------------------
    # _listing validation (lines 208, 211, 221, 227)
    # ------------------------------------------------------------------

    def test_remote_indicator_must_be_boolean(self):
        source_obj = source()
        entry = make_entry(remote="yes")
        http, _ = http_for([json_response(payload([entry]))])
        with pytest.raises(errors.SchemaDriftError, match="RemoteIndicator must be boolean"):
            self.adapter(http, source_obj).fetch(JobSourceQuery())

    def test_remuneration_must_be_list_of_objects(self):
        source_obj = source()
        entry = make_entry(remuneration="not-a-list")
        http, _ = http_for([json_response(payload([entry]))])
        with pytest.raises(errors.SchemaDriftError, match="PositionRemuneration must be a list"):
            self.adapter(http, source_obj).fetch(JobSourceQuery())

    def test_user_area_details_must_be_object(self):
        source_obj = source()
        entry = make_entry(description="", user_area={"Details": "not-an-object"})
        http, _ = http_for([json_response(payload([entry]))])
        with pytest.raises(errors.SchemaDriftError, match="UserArea.Details must be an object"):
            self.adapter(http, source_obj).fetch(JobSourceQuery())

    def test_position_status_must_be_string(self):
        source_obj = source()
        entry = make_entry(status=42)
        http, _ = http_for([json_response(payload([entry]))])
        with pytest.raises(errors.SchemaDriftError, match="PositionStatus must be a string"):
            self.adapter(http, source_obj).fetch(JobSourceQuery())

    # ------------------------------------------------------------------
    # Missing MatchedObjectDescriptor
    # ------------------------------------------------------------------

    def test_missing_descriptor(self):
        source_obj = source()
        http, _ = http_for(
            [json_response({"SearchResult": {"SearchResultItems": [{"MatchedObjectId": "x"}]}})]
        )
        with pytest.raises(errors.SchemaDriftError, match="MatchedObjectDescriptor"):
            self.adapter(http, source_obj).fetch(JobSourceQuery())

    # ------------------------------------------------------------------
    # Description fallback through UserArea.Details.JobSummary
    # ------------------------------------------------------------------

    def test_description_fallback_to_user_area_details(self):
        source_obj = source()
        entry = make_entry(description="", user_area={"Details": {"JobSummary": "Fallback summary."}})
        http, _ = http_for([json_response(payload([entry]))])
        result = self.adapter(http, source_obj).fetch(JobSourceQuery())
        assert result.listings[0].description_excerpt == "Fallback summary."
