# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Guardrails for the Phase 3 external job-source catalog."""

import copy
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
    "ssrf_allowlist",
    "canonical_hosts",
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


def assert_https_url(url, field, record):
    assert isinstance(url, str) and url, f"{field} must be a non-empty string for '{record}'"
    parsed = urlparse(url)
    assert parsed.scheme == "https", f"{field} must use https for '{record}': {url}"
    assert parsed.netloc, f"{field} must have a host for '{record}': {url}"


def assert_optional_https_url(url, field, record):
    if url is not None:
        assert_https_url(url, field, record)


def _check_at_most_one_approved(catalog):
    approved = [
        source
        for source in catalog["sources"]
        if source["approval_state"] == "approved" and source["kind"] == "external"
    ]
    assert len(approved) <= 1, f"expected at most one approved external source, got {len(approved)}"
    if approved:
        assert approved[0]["name"].lower() == catalog["mvp"]["source"].lower()


def _check_source_ssrf_policy(catalog):
    global_allowlist = set(catalog["ssrf"]["allowlist"])
    assert catalog["ssrf"]["require_https"] is True
    for source in catalog["sources"]:
        source_allowlist = set(source["ssrf_allowlist"])
        assert source_allowlist <= global_allowlist, source["name"]
        for field in ("url", "api.endpoint", "api.docs", "api.terms_url"):
            if field == "url":
                url = source["url"]
            else:
                section, key = field.split(".")
                url = source[section].get(key)
            if not url or not source_allowlist:
                continue
            assert urlparse(url).hostname in source_allowlist, (
                f"{source['name']} {field} host is not in source SSRF allowlist"
            )
        for canonical_host in source.get("canonical_hosts", []):
            if source_allowlist:
                assert canonical_host in source_allowlist, (
                    f"{source['name']} canonical host is not in source SSRF allowlist"
                )
        if source["approval_state"] == "approved":
            assert source_allowlist, f"approved source {source['name']} needs a source SSRF allowlist"


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

        assert_https_url(source["url"], "url", name)
        assert source["kind"] in ALLOWED_KINDS, f"{name} has invalid kind"
        assert source["approval_state"] in ALLOWED_APPROVAL_STATES
        approval = source["approval"]
        assert approval["state"] == source["approval_state"]
        assert approval["owner"]
        assert approval["review_date"]
        assert isinstance(approval["evidence"], list)
        for evidence_url in approval["evidence"]:
            assert_https_url(evidence_url, "approval.evidence[]", name)

        api = source["api"]
        missing_api = REQUIRED_API_KEYS - api.keys()
        assert not missing_api, f"{name}.api is missing keys: {sorted(missing_api)}"
        assert_optional_https_url(api["endpoint"], "api.endpoint", name)
        assert_optional_https_url(api["docs"], "api.docs", name)
        assert_optional_https_url(api["terms_url"], "api.terms_url", name)
        assert isinstance(source["compensation_fields"], list)
        assert isinstance(source["location_fields"], list)
        assert isinstance(source["matching_fields"], list)
        assert isinstance(source["job_text"]["retain"], bool)
        assert isinstance(source["job_text"]["display"], bool)
        assert source["live_enabled"] is False


def test_at_most_one_approved_external_mvp_source(catalog):
    _check_at_most_one_approved(catalog)


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


def test_source_ssrf_policy_covers_request_and_canonical_hosts(catalog):
    _check_source_ssrf_policy(catalog)


def test_approved_source_branches_with_mock(catalog):
    """Exercise the approved-source conditional branches that are unreachable
    when no real source is approved."""
    mock = copy.deepcopy(catalog)
    first = mock["sources"][0]
    first["approval_state"] = "approved"
    first["approval"]["state"] = "approved"
    first["url"] = "https://example.com/"
    first["api"]["endpoint"] = "https://example.com/api"
    first["api"]["docs"] = "https://example.com/docs"
    first["api"]["terms_url"] = "https://example.com/terms"
    first["ssrf_allowlist"] = ["example.com"]
    first["canonical_hosts"] = ["example.com"]
    mock["mvp"]["source"] = first["name"]
    mock["ssrf"]["allowlist"] = ["example.com"]
    mock["mvp"]["ssrf_allowlist"] = ["example.com"]

    _check_at_most_one_approved(mock)
    _check_source_ssrf_policy(mock)
