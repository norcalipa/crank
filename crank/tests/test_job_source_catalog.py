# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Guardrails for the Phase 3 external job-source catalog."""

from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "docs" / "job-source-catalog.yaml"

ALLOWED_APPROVAL_STATES = {"approved", "pending", "blocked"}
ALLOWED_KINDS = {"external"}
REQUIRED_REVIEW_KEYS = {"document", "issue", "review_date", "reviewed_by", "decision_owner"}
REQUIRED_SOURCE_KEYS = {
    "name",
    "url",
    "kind",
    "approval_state",
    "approval",
    "api",
    "canonical_id",
    "canonical_url",
    "compensation_fields",
    "location_fields",
    "matching_fields",
    "allowed_matching_use",
    "allowed_presentation_use",
    "job_text",
    "retention",
    "deletion_expiry_obligations",
    "live_enabled",
}
REQUIRED_API_KEYS = {
    "available",
    "endpoint",
    "docs",
    "authentication",
    "cost",
    "terms_url",
    "robots",
    "rate_limits",
    "pagination",
}


def load_yaml(path):
    assert path.exists(), f"missing catalog source: {path}"
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    assert isinstance(value, dict), f"catalog must contain a YAML mapping: {path}"
    return value


def assert_http_url(url, field, record):
    assert isinstance(url, str) and url, f"{field} must be a non-empty string for '{record}'"
    parsed = urlparse(url)
    assert parsed.scheme in {"http", "https"}, f"{field} must be http(s) for '{record}': {url}"
    assert parsed.netloc, f"{field} must have a host for '{record}': {url}"


def assert_optional_http_url(url, field, record):
    if url is not None:
        assert_http_url(url, field, record)


@pytest.fixture(scope="module")
def catalog():
    return load_yaml(CATALOG)


def test_catalog_parseable_and_shaped(catalog):
    assert catalog["review"].keys() >= REQUIRED_REVIEW_KEYS
    assert catalog["review"]["issue"] == 316
    assert catalog["mvp"]["source"]
    assert catalog["mvp"]["live_enabled"] is False
    assert catalog["live_enabled"] is False
    assert catalog["mvp"]["ssrf_allowlist"]
    assert catalog["data_classification"]["approved_mvp"]
    assert isinstance(catalog["sources"], list) and catalog["sources"]


def test_source_records_are_well_formed(catalog):
    names = set()
    for source in catalog["sources"]:
        name = source.get("name")
        assert name and name not in names, f"source names must be unique: {name}"
        names.add(name)
        missing = REQUIRED_SOURCE_KEYS - source.keys()
        assert not missing, f"{name} is missing required keys: {sorted(missing)}"

        assert_http_url(source["url"], "url", name)
        assert source["kind"] in ALLOWED_KINDS, f"{name} has invalid kind"
        assert source["approval_state"] in ALLOWED_APPROVAL_STATES
        approval = source["approval"]
        assert approval["state"] == source["approval_state"]
        assert approval["owner"]
        assert approval["review_date"]
        assert isinstance(approval["evidence"], list)
        for evidence_url in approval["evidence"]:
            assert_http_url(evidence_url, "approval.evidence[]", name)

        api = source["api"]
        missing_api = REQUIRED_API_KEYS - api.keys()
        assert not missing_api, f"{name}.api is missing keys: {sorted(missing_api)}"
        assert_optional_http_url(api["endpoint"], "api.endpoint", name)
        assert_optional_http_url(api["docs"], "api.docs", name)
        assert_optional_http_url(api["terms_url"], "api.terms_url", name)
        assert isinstance(source["compensation_fields"], list)
        assert isinstance(source["location_fields"], list)
        assert isinstance(source["matching_fields"], list)
        assert isinstance(source["job_text"]["retain"], bool)
        assert isinstance(source["job_text"]["display"], bool)
        assert source["live_enabled"] is False


def test_exactly_one_approved_external_mvp_source(catalog):
    approved = [
        source
        for source in catalog["sources"]
        if source["approval_state"] == "approved" and source["kind"] == "external"
    ]
    assert len(approved) == 1, f"expected exactly one approved external source, got {len(approved)}"
    assert approved[0]["name"].lower() == catalog["mvp"]["source"].lower()


def test_mvp_source_is_not_live_by_default(catalog):
    mvp_name = catalog["mvp"]["source"].lower()
    mvp_sources = [source for source in catalog["sources"] if source["name"].lower() == mvp_name]
    assert len(mvp_sources) == 1
    assert catalog["live_enabled"] is False
    assert catalog["mvp"]["live_enabled"] is False
    assert mvp_sources[0]["live_enabled"] is False


def test_ssrf_allowlist_contains_valid_bare_hostnames(catalog):
    allowlist = catalog["ssrf"]["allowlist"]
    assert allowlist
    assert allowlist == catalog["mvp"]["ssrf_allowlist"]
    for host in allowlist:
        assert isinstance(host, str) and host
        assert "@" not in host
        assert "://" not in host
        assert "/" not in host
        assert "?" not in host
        assert "#" not in host
        assert "." in host
        parsed = urlparse(f"https://{host}")
        assert parsed.hostname == host
        assert parsed.port is None
