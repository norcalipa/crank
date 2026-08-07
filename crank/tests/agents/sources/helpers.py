# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Test helpers for the phase-2 source adapter suite.

These drive :class:`SafeHTTPClient` and :class:`YelpSourceAdapter` through
injected request/resolver seams and recorded, sanitized fixtures. No live
network call ever happens in the test suite.
"""
from __future__ import annotations

import io
from typing import Iterable, Mapping, Optional

import requests


class FakeResponse:
    """Minimal stand-in for ``requests.Response`` used by transport tests.

    ``iter_content`` yields full ``content`` in one chunk (transports never
    rely on chunking); ``raw`` is a seekable buffer for compat.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: Optional[Mapping[str, str]] = None,
        content: bytes = b"",
        url: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.content = content
        self.url = url
        self.raw = io.BytesIO(content)

    def iter_content(self, chunk_size: int = 65536):
        yield self.content


def fake_requests_factory(responses: Iterable[FakeResponse], *, url: Optional[str] = None):
    """Return a callable that replays ``responses`` in order as requests.

    Each invocation returns the next response in ``responses``; when exhausted
    the last response repeats (used by retry tests).
    """
    responses = list(responses)

    def fake_request(method, url_arg, **kwargs):
        assert method.upper() == "GET"
        url_actual = url_arg
        resp = responses[0]
        if len(responses) > 1:
            responses.pop(0)
        resp.url = url_actual or resp.url
        return resp

    return fake_request


def load_fixture(name: str) -> bytes:
    """Read a sanitized fixture file under ``.../fixtures/sources/yelp``."""
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent.parent / "fixtures" / "sources" / "yelp"
    return (base / name).read_bytes()
