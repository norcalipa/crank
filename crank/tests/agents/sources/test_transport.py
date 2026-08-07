# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the hardened HTTP transport (no live network)."""
from __future__ import annotations

import pytest
import requests

from crank.agents.sources import errors
from crank.agents.sources.transport import SafeHTTPClient, address_is_blocked
from crank.tests.agents.sources.helpers import FakeResponse, fake_requests_factory

ALLOWED = ("api.yelp.com",)
BASE = "https://api.yelp.com/v3/businesses/search"


def make_client(*args, **kwargs):
    kwargs.setdefault("allowed_hosts", ALLOWED)
    kwargs.setdefault("expected_content_type", "application/json")
    return SafeHTTPClient(*args, **kwargs)


class TestAllowlistAndScheme:
    def test_empty_allowed_hosts_raises(self):
        with pytest.raises(ValueError, match="allowed_hosts must be non-empty"):
            SafeHTTPClient(allowed_hosts=[], expected_content_type="application/json")

    def test_non_https_rejected(self):
        client = make_client(use_requests=fake_requests_factory([FakeResponse(status_code=200)]))
        with pytest.raises(errors.BlockedRedirectError):
            client.get("http://api.yelp.com/v3/businesses/search")

    def test_host_not_in_allowlist_rejected(self):
        client = make_client(use_requests=fake_requests_factory([FakeResponse(status_code=200)]))
        with pytest.raises(errors.BlockedRedirectError):
            client.get("https://evil.example.com/v3/businesses/search")

    def test_subdomain_not_allowlisted(self):
        client = make_client(use_requests=fake_requests_factory([FakeResponse(status_code=200)]))
        with pytest.raises(errors.BlockedRedirectError):
            client.get("https://api.yelp.com.evil.example/v3")


class TestAddressBlocking:
    def test_loopback_blocked_after_dns(self):
        client = make_client(
            use_requests=fake_requests_factory([FakeResponse(status_code=200)]),
            resolver=lambda host: ["127.0.0.1"],
        )
        with pytest.raises(errors.BlockedAddressError):
            client.get(BASE)

    def test_link_local_blocked(self):
        client = make_client(resolver=lambda host: ["169.254.169.254"])
        with pytest.raises(errors.BlockedAddressError):
            client.get(BASE)

    def test_private_cgnat_blocked(self):
        client = make_client(resolver=lambda host: ["100.64.0.1"])
        with pytest.raises(errors.BlockedAddressError):
            client.get(BASE)

    def test_public_address_allowed(self):
        called = []

        def fake(method, url, **kwargs):
            called.append((method, url))
            return FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=b"{}")

        client = make_client(use_requests=fake, resolver=lambda host: ["172.16.0.1"] if host == "bad" else ["8.8.8.8"])
        status, _, body = client.get(BASE)
        assert status == 200
        assert body == b"{}"

    def test_address_is_blocked_literals(self):
        assert address_is_blocked("127.0.0.1")
        assert address_is_blocked("10.0.0.1")
        assert address_is_blocked("::1")
        assert address_is_blocked("fe80::1")
        assert not address_is_blocked("8.8.8.8")
        assert not address_is_blocked("2606:4700:4700::1111")
        assert address_is_blocked("not-an-ip")


class TestBodiesAndContentType:
    def test_wrong_content_type(self):
        client = make_client(
            use_requests=fake_requests_factory(
                [
                    FakeResponse(
                        status_code=200,
                        headers={"Content-Type": "text/html; charset=utf-8"},
                        content=b"<html></html>",
                    )
                ]
            )
        )
        with pytest.raises(errors.WrongContentTypeError):
            client.get(BASE)

    def test_oversized_body(self):
        big = b"x" * 100
        client = make_client(
            max_bytes=50,
            use_requests=fake_requests_factory(
                [FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=big)]
            ),
        )
        with pytest.raises(errors.OversizedPayloadError):
            client.get(BASE)

    def test_missing_content_type(self):
        client = make_client(use_requests=fake_requests_factory([FakeResponse(status_code=200, content=b"{}")]))
        with pytest.raises(errors.WrongContentTypeError):
            client.get(BASE)

    def test_body_returned(self):
        body = b'{"ok": true}'
        client = make_client(
            use_requests=fake_requests_factory(
                [FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=body)]
            )
        )
        _, _, returned = client.get(BASE)
        assert returned == body


class TestRedirects:
    def test_redirect_to_unapproved_host_blocked(self):
        client = make_client(
            use_requests=fake_requests_factory(
                [
                    FakeResponse(status_code=302, headers={"Location": "https://evil.example.com/"}, content=b""),
                    FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=b"{}"),
                ]
            )
        )
        with pytest.raises(errors.BlockedRedirectError):
            client.get(BASE)

    def test_http_redirect_blocked(self):
        client = make_client(
            use_requests=fake_requests_factory(
                [
                    FakeResponse(status_code=301, headers={"Location": "http://api.yelp.com/other"}, content=b""),
                    FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=b"{}"),
                ]
            )
        )
        with pytest.raises(errors.BlockedRedirectError):
            client.get(BASE)

    def test_redirect_to_private_address_blocked(self):
        client = make_client(
            resolver=lambda host: ["10.0.0.5"],
            use_requests=fake_requests_factory(
                [
                    FakeResponse(status_code=302, headers={"Location": "https://api.yelp.com/other"}, content=b""),
                    FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=b"{}"),
                ]
            ),
        )
        with pytest.raises(errors.BlockedAddressError):
            client.get(BASE)

    def test_allowed_redirect_followed(self):
        calls = []

        def fake(method, url, **kwargs):
            calls.append(url)
            if url == BASE:
                return FakeResponse(status_code=302, headers={"Location": "https://api.yelp.com/v3/next"}, content=b"")
            return FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=b"{}")

        client = make_client(use_requests=fake, resolver=lambda host: ["8.8.8.8"])
        status, _, body = client.get(BASE)
        assert status == 200
        assert body == b"{}"
        assert calls == [BASE, "https://api.yelp.com/v3/next"]

    def test_redirect_loop_detected(self):
        client = make_client(
            resolver=lambda host: ["8.8.8.8"],
            max_redirects=5,
            use_requests=fake_requests_factory(
                [
                    FakeResponse(status_code=302, headers={"Location": "https://api.yelp.com/a"}, content=b""),
                    FakeResponse(status_code=302, headers={"Location": "https://api.yelp.com/b"}, content=b""),
                ]
            ),
        )
        with pytest.raises(errors.BlockedRedirectError):
            client.get(BASE)

    def test_redirect_without_location(self):
        client = make_client(
            resolver=lambda host: ["8.8.8.8"],
            use_requests=fake_requests_factory([FakeResponse(status_code=302, content=b"")]),
        )
        with pytest.raises(errors.MalformedPayloadError):
            client.get(BASE)

    def test_redirect_count_exhausted(self):
        # Every hop redirects to another allowlisted URL; bounded count trips.
        client = make_client(
            resolver=lambda host: ["8.8.8.8"],
            max_redirects=2,
            use_requests=fake_requests_factory(
                [
                    FakeResponse(status_code=302, headers={"Location": "https://api.yelp.com/a"}, content=b""),
                    FakeResponse(status_code=302, headers={"Location": "https://api.yelp.com/b"}, content=b""),
                    FakeResponse(status_code=302, headers={"Location": "https://api.yelp.com/c"}, content=b""),
                ]
            ),
        )
        with pytest.raises(errors.BlockedRedirectError):
            client.get(BASE)

    def test_multiple_blocked_addresses_reported(self):
        client = make_client(
            resolver=lambda host: ["127.0.0.1", "10.0.0.5"],
            use_requests=fake_requests_factory([FakeResponse(status_code=200)]),
        )
        with pytest.raises(errors.BlockedAddressError) as excinfo:
            client.get(BASE)
        msg = str(excinfo.value)
        assert "127.0.0.1" in msg and "10.0.0.5" in msg


class TestStatusCodes:
    def test_unauthorized(self):
        client = make_client(use_requests=fake_requests_factory([FakeResponse(status_code=401, content=b"")]))
        with pytest.raises(errors.UnauthorizedSourceError):
            client.get(BASE)

    def test_forbidden(self):
        client = make_client(use_requests=fake_requests_factory([FakeResponse(status_code=403, content=b"")]))
        with pytest.raises(errors.UnauthorizedSourceError):
            client.get(BASE)

    def test_unexpected_status_code_300(self):
        client = make_client(
            use_requests=fake_requests_factory([FakeResponse(status_code=300, content=b"")]),
            resolver=lambda host: ["8.8.8.8"],
            max_transient_attempts=1,
        )
        with pytest.raises(errors.RetriesExhaustedError) as excinfo:
            client.get(BASE)
        assert isinstance(excinfo.value.last_error, errors.SourceServerError)

    def test_urlparse_value_error(self, monkeypatch):
        import crank.agents.sources.transport as tp

        def bad_parse(url):
            raise ValueError("invalid format")

        client = make_client(use_requests=fake_requests_factory([FakeResponse(status_code=200, headers={"Content-Type": "application/json"})]))
        monkeypatch.setattr(tp, "urlparse", bad_parse)
        with pytest.raises(errors.SourceServerError, match="invalid URL"):
            client.get(BASE)



class TestRetries:
    def test_5xx_retries_then_success(self):
        responses = [
            FakeResponse(status_code=500, content=b""),
            FakeResponse(status_code=502, content=b""),
            FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=b"{}"),
        ]
        client = make_client(
            use_requests=fake_requests_factory(responses),
            resolver=lambda host: ["8.8.8.8"],
            max_transient_attempts=4,
            backoff_base=0.01,
        )
        status, _, _ = client.get(BASE)
        assert status == 200

    def test_5xx_retries_exhausted(self):
        client = make_client(
            use_requests=fake_requests_factory([FakeResponse(status_code=500, content=b"")]),
            max_transient_attempts=3,
            backoff_base=0.01,
        )
        with pytest.raises(errors.RetriesExhaustedError):
            client.get(BASE)

    def test_429_uses_retry_after_then_success(self):
        responses = [
            FakeResponse(status_code=429, headers={"Retry-After": "1"}, content=b""),
            FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=b"{}"),
        ]
        client = make_client(
            use_requests=fake_requests_factory(responses),
            resolver=lambda host: ["8.8.8.8"],
            max_transient_attempts=3,
            backoff_base=0.01,
        )
        status, _, _ = client.get(BASE)
        assert status == 200

    def test_429_exhausted(self):
        client = make_client(
            use_requests=fake_requests_factory([FakeResponse(status_code=429, content=b"")]),
            max_transient_attempts=2,
            backoff_base=0.01,
        )
        with pytest.raises(errors.RetriesExhaustedError):
            client.get(BASE)

    def test_timeout_then_success(self):
        def fake(method, url, **kwargs):
            nonlocal called
            called += 1
            if called == 1:
                raise requests.Timeout("timed out")
            return FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=b"{}")

        called = 0
        client = make_client(use_requests=fake, resolver=lambda host: ["8.8.8.8"], max_transient_attempts=3, backoff_base=0.01)
        status, _, _ = client.get(BASE)
        assert status == 200

    def test_timeout_exhausted(self):
        def fake(method, url, **kwargs):
            raise requests.Timeout("timed out")

        client = make_client(use_requests=fake, max_transient_attempts=3, backoff_base=0.01)
        with pytest.raises(errors.RetriesExhaustedError):
            client.get(BASE)

    def test_connection_error_classified_as_server(self):
        def fake(method, url, **kwargs):
            raise requests.ConnectionError("refused")

        client = make_client(use_requests=fake, max_transient_attempts=1, backoff_base=0.01)
        with pytest.raises(errors.RetriesExhaustedError) as excinfo:
            client.get(BASE)
        assert isinstance(excinfo.value.last_error, errors.SourceServerError)

    def test_generic_request_error_classified_as_server(self):
        def fake(method, url, **kwargs):
            raise requests.RequestException("misc")

        client = make_client(use_requests=fake, max_transient_attempts=1, backoff_base=0.01)
        with pytest.raises(errors.RetriesExhaustedError) as excinfo:
            client.get(BASE)
        assert isinstance(excinfo.value.last_error, errors.SourceServerError)

    def test_invalid_retry_after_falls_back_to_backoff(self):
        responses = [
            FakeResponse(status_code=429, headers={"Retry-After": "not-a-number"}, content=b""),
            FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=b"{}"),
        ]
        client = make_client(
            use_requests=fake_requests_factory(responses),
            resolver=lambda host: ["8.8.8.8"],
            max_transient_attempts=3,
            backoff_base=0.01,
        )
        status, _, _ = client.get(BASE)
        assert status == 200

    def test_redact_auth_never_leaks_bearer(self):
        from crank.agents.sources.transport import redact_auth

        redacted = redact_auth("Authorization: Bearer sekret")
        assert "sekret" not in redacted
        assert "Authorization: ***" in redacted


class TestAuthRedaction:
    def test_auth_header_sent_but_never_in_errors(self):
        received = {}

        def fake(method, url, headers=None, **kwargs):
            received["headers"] = dict(headers or {})
            raise requests.Timeout("boom")

        client = make_client(
            use_requests=fake,
            max_transient_attempts=1,
            auth_headers={"Authorization": "Bearer super-secret-key"},
        )
        with pytest.raises(errors.RetriesExhaustedError) as excinfo:
            client.get(BASE)
        assert received["headers"]["Authorization"] == "Bearer super-secret-key"
        assert "super-secret-key" not in str(excinfo.value)

    def test_fetch_with_custom_headers(self):
        received = {}

        def fake(method, url, headers=None, **kwargs):
            received["headers"] = dict(headers or {})
            return FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, content=b"{}")

        client = make_client(use_requests=fake, resolver=lambda host: ["8.8.8.8"])
        client.get(BASE, headers={"X-Custom-Header": "custom_value"})
        assert received["headers"]["X-Custom-Header"] == "custom_value"
