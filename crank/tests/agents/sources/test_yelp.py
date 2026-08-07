# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the Yelp Fusion source adapter (no live network).

Every test drives :class:`YelpSourceAdapter` through an injected
:class:`SafeHTTPClient` whose ``requests`` and DNS-resolver seams are fakes,
proving the full transport security path without ever touching the network.
Hand-crafted responses reuse the sanitized recorded fixtures under
``crank/tests/fixtures/sources/yelp/``.
"""
from __future__ import annotations

import json

import pytest

from crank.agents.sources import errors
from crank.agents.sources.contract import SourceQuery
from crank.agents.sources.transport import SafeHTTPClient
from crank.agents.sources.yelp import YelpSourceAdapter
from crank.tests.agents.sources.helpers import FakeResponse, load_fixture

ALLOWED_HOSTS = ("api.yelp.com",)
PUBLIC_RESOLVER = lambda host: ["8.8.8.8"]  # noqa: E731


def json_response(body: bytes, *, status: int = 200, headers=None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return FakeResponse(status_code=status, headers=h, content=body)


def fixture_bytes(name: str) -> bytes:
    return load_fixture(name)


def fixture_response(name: str, *, status: int = 200):
    return json_response(fixture_bytes(name), status=status)


def recording_factory(responses):
    """Replay ``responses`` in order and record each request call."""
    responses = list(responses)
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, url, kwargs.get("params")))
        resp = responses[0]
        if len(responses) > 1:
            responses.pop(0)
        resp.url = url
        return resp

    return fake, calls


def make_adapter(responses, *, max_pages=20, max_observations=500, **transport_kw):
    fake, calls = recording_factory(responses)
    http = SafeHTTPClient(
        allowed_hosts=ALLOWED_HOSTS,
        expected_content_type="application/json",
        use_requests=fake,
        resolver=PUBLIC_RESOLVER,
        max_transient_attempts=transport_kw.pop("max_transient_attempts", 3),
        backoff_base=0.01,
        auth_headers={"Authorization": "Bearer test-key"},
        **transport_kw,
    )
    adapter = YelpSourceAdapter(
        api_key="test-key",
        http=http,
        base_url="https://api.yelp.com/v3",
        max_pages=max_pages,
        max_observations=max_observations,
    )
    return adapter, calls


QUERY = SourceQuery(term="fixtures", location="San Francisco")


class TestHappyPath:
    def test_fixture_page_produces_typed_observations(self):
        adapter, _ = make_adapter([fixture_response("search_page1.json")])
        result = adapter.fetch(QUERY)

        assert result.pages_fetched == 1
        assert result.items_seen == 2
        assert len(result.observations) == 2

        first = result.observations[0]
        assert first.external_id == "sanitized-fixture-acme-1"
        assert first.target_identity == "Fixture Corp"
        assert first.score_type == "rating"
        assert first.value == 4.1
        assert first.range_low == 0.0
        assert first.range_high == 5.0
        assert first.source_url == "https://www.yelp.com/biz/fixture-corp-sanfrancisco"
        assert first.adapter == "yelp"
        assert first.adapter_version == "1.0.0"
        assert first.observed_at.tzinfo is not None
        assert first.fetched_at.tzinfo is not None

        second = result.observations[1]
        assert second.external_id == "sanitized-fixture-globex-2"
        assert second.target_identity == "Globex Labs"
        assert second.value == 3.7

    def test_empty_response_yields_no_observations(self):
        adapter, _ = make_adapter([fixture_response("search_empty.json")])
        result = adapter.fetch(QUERY)
        assert result.pages_fetched == 1
        assert result.items_seen == 0
        assert result.observations == []


class TestPagination:
    def test_multiple_pages_merge_and_respect_offset(self):
        # First page reports a total larger than one page so the adapter keeps
        # paginating; the sanitized page-2 fixture supplies the second page.
        page1 = json.loads(fixture_bytes("search_page1.json"))
        page1["total"] = 60
        first = json_response(json.dumps(page1).encode("utf-8"))
        second = fixture_response("search_page2.json")

        adapter, calls = make_adapter([first, second], max_observations=500)
        result = adapter.fetch(QUERY)

        assert result.pages_fetched == 2
        assert result.items_seen == 3
        assert len(result.observations) == 3
        ids = [obs.external_id for obs in result.observations]
        assert ids == [
            "sanitized-fixture-acme-1",
            "sanitized-fixture-globex-2",
            "sanitized-fixture-umbrella-3",
        ]

        assert calls[0][1] == "https://api.yelp.com/v3/businesses/search"
        assert calls[0][2]["offset"] == 0
        assert calls[1][2]["offset"] == 50
        assert calls[0][2]["limit"] == 50

    def test_pagination_bounded_by_max_pages(self):
        page1 = json.loads(fixture_bytes("search_page1.json"))
        page1["total"] = 60
        first = json_response(json.dumps(page1).encode("utf-8"))
        second = fixture_response("search_page2.json")

        adapter, calls = make_adapter([first, second], max_pages=1, max_observations=500)
        result = adapter.fetch(QUERY)

        assert result.pages_fetched == 1
        assert len(result.observations) == 2
        assert len(calls) == 1

    def test_pagination_bounded_by_observation_budget(self):
        # A small observation budget stops even though the source reports more
        # results are available; the transport never over-fetches its budget.
        page1 = json.loads(fixture_bytes("search_page1.json"))
        page1["total"] = 1000
        first = json_response(json.dumps(page1).encode("utf-8"))
        page2 = json.loads(fixture_bytes("search_page2.json"))
        page2["total"] = 1000
        second = json_response(json.dumps(page2).encode("utf-8"))

        adapter, calls = make_adapter([first, second], max_pages=20, max_observations=90)
        result = adapter.fetch(QUERY)

        # offset crosses the 90-item budget after the second page and stops.
        assert result.pages_fetched == 2
        assert len(result.observations) == 3
        assert len(calls) == 2


class TestAuthentication:
    def test_unauthorized_propagates(self):
        adapter, _ = make_adapter([json_response(b"", status=401)])
        with pytest.raises(errors.UnauthorizedSourceError):
            adapter.fetch(QUERY)

    def test_forbidden_propagates(self):
        adapter, _ = make_adapter([json_response(b"", status=403)])
        with pytest.raises(errors.UnauthorizedSourceError):
            adapter.fetch(QUERY)

    def test_fails_closed_without_api_key(self):
        with pytest.raises(errors.UnauthorizedSourceError):
            YelpSourceAdapter()  # no env key, no injected http


class TestTransientFailures:
    def test_429_retry_exhausted(self):
        adapter, _ = make_adapter(
            [json_response(b"", status=429)],
            max_transient_attempts=2,
        )
        with pytest.raises(errors.RetriesExhaustedError) as excinfo:
            adapter.fetch(QUERY)
        assert isinstance(excinfo.value.last_error, errors.SourceThrottledError)

    def test_5xx_retry_exhausted(self):
        adapter, _ = make_adapter(
            [json_response(b"", status=500)],
            max_transient_attempts=2,
        )
        with pytest.raises(errors.RetriesExhaustedError) as excinfo:
            adapter.fetch(QUERY)
        assert isinstance(excinfo.value.last_error, errors.SourceServerError)

    def test_timeout_retry_exhausted(self):
        import requests

        def always_timeout(method, url, **kwargs):
            raise requests.Timeout("timed out")

        calls = []
        http = SafeHTTPClient(
            allowed_hosts=ALLOWED_HOSTS,
            expected_content_type="application/json",
            use_requests=always_timeout,
            resolver=PUBLIC_RESOLVER,
            max_transient_attempts=2,
            backoff_base=0.01,
        )
        adapter = YelpSourceAdapter(api_key="test-key", http=http, base_url="https://api.yelp.com/v3")
        with pytest.raises(errors.RetriesExhaustedError) as excinfo:
            adapter.fetch(QUERY)
        assert isinstance(excinfo.value.last_error, errors.SourceTimeoutError)
        assert not calls


class TestParseAndContent:
    def test_malformed_payload(self):
        adapter, _ = make_adapter([json_response(fixture_bytes("search_malformed.json"))])
        with pytest.raises(errors.MalformedPayloadError):
            adapter.fetch(QUERY)

    def test_oversized_response(self):
        big = json.dumps({"businesses": [], "total": 0}).encode("utf-8") + b"x" * 2048
        adapter, _ = make_adapter(
            [json_response(big)],
            max_bytes=512,
        )
        with pytest.raises(errors.OversizedPayloadError):
            adapter.fetch(QUERY)

    def test_wrong_content_type(self):
        adapter, _ = make_adapter(
            [
                FakeResponse(
                    status_code=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    content=b"<html></html>",
                )
            ]
        )
        with pytest.raises(errors.WrongContentTypeError):
            adapter.fetch(QUERY)

    def test_schema_drift(self):
        adapter, _ = make_adapter([fixture_response("search_schema_drift.json")])
        with pytest.raises(errors.SchemaDriftError):
            adapter.fetch(QUERY)

    @pytest.mark.parametrize(
        "payload",
        [
            # top level not an object
            b"[1, 2, 3]",
            # businesses not a list
            json.dumps({"businesses": {"id": "x"}, "total": 1}).encode("utf-8"),
            # business not an object
            json.dumps({"businesses": ["not-a-dict"], "total": 1}).encode("utf-8"),
            # missing id
            json.dumps(
                {"businesses": [{"name": "X", "rating": 4.0, "url": "https://x"}], "total": 1}
            ).encode("utf-8"),
            # missing name
            json.dumps(
                {"businesses": [{"id": "a", "rating": 4.0, "url": "https://x"}], "total": 1}
            ).encode("utf-8"),
            # non-numeric rating
            json.dumps(
                {"businesses": [{"id": "a", "name": "X", "rating": "high", "url": "https://x"}], "total": 1}
            ).encode("utf-8"),
            # rating out of range
            json.dumps(
                {"businesses": [{"id": "a", "name": "X", "rating": 6.0, "url": "https://x"}], "total": 1}
            ).encode("utf-8"),
            # missing url
            json.dumps(
                {"businesses": [{"id": "a", "name": "X", "rating": 4.0}], "total": 1}
            ).encode("utf-8"),
            # total not an int
            json.dumps({"businesses": [], "total": "many"}).encode("utf-8"),
            # total negative
            json.dumps({"businesses": [], "total": -1}).encode("utf-8"),
        ],
        ids=[
            "top-level-not-object",
            "businesses-not-list",
            "business-not-object",
            "missing-id",
            "missing-name",
            "non-numeric-rating",
            "rating-out-of-range",
            "missing-url",
            "total-not-int",
            "total-negative",
        ],
    )
    def test_schema_drift_variants(self, payload):
        adapter, _ = make_adapter([json_response(payload)])
        with pytest.raises(errors.SchemaDriftError):
            adapter.fetch(QUERY)


class TestRedirects:
    def test_redirect_to_unapproved_host(self):
        adapter, _ = make_adapter(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "https://evil.example.com/"},
                    content=b"",
                )
            ]
        )
        with pytest.raises(errors.BlockedRedirectError):
            adapter.fetch(QUERY)

    def test_http_redirect(self):
        adapter, _ = make_adapter(
            [
                FakeResponse(
                    status_code=301,
                    headers={"Location": "http://api.yelp.com/other"},
                    content=b"",
                )
            ]
        )
        with pytest.raises(errors.BlockedRedirectError):
            adapter.fetch(QUERY)


class TestNoLiveNetwork:
    def test_injected_client_is_used_not_requests_request(self, monkeypatch):
        import requests

        def boom(*args, **kwargs):
            raise AssertionError("live network call attempted in test suite")

        monkeypatch.setattr(requests, "request", boom)

        adapter, calls = make_adapter([fixture_response("search_page1.json")])
        result = adapter.fetch(QUERY)
        assert result.pages_fetched == 1
        assert len(result.observations) == 2
        assert calls, "injected request callable was not used"
