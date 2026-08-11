# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Guardrails for the Phase 3 external job-source catalog."""

import copy
import datetime as dt
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "docs" / "job-source-catalog.yaml"

ALLOWED_APPROVAL_STATES = {"approved", "pending", "blocked"}
ALLOWED_KINDS = {"external"}
REQUIRED_REVIEW_KEYS = {
    "document",
    "issue",
    "review_date",
    "reviewed_by",
    "decision_owner",
}
REQUIRED_SOURCE_KEYS = {
    "name",
    "url",
    "kind",
    "approval_state",
    "approval",
    "api",
    "canonical_id",
    "canonical_url",
    "canonical_hosts",
    "compensation_fields",
    "location_fields",
    "matching_fields",
    "allowed_matching_use",
    "allowed_presentation_use",
    "effective_permissions",
    "ingestion_policy",
    "job_text",
    "retention",
    "deletion_expiry_obligations",
    "live_enabled",
    "ssrf",
    "operational_controls",
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
BARE_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def load_yaml(path):
    assert path.exists(), f"missing catalog source: {path}"
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    assert isinstance(value, dict), f"catalog must contain a YAML mapping: {path}"
    return value


def assert_bare_hostname(host, field, record):
    assert isinstance(host, str) and BARE_HOST_RE.fullmatch(host), (
        f"{field} must be a bare hostname for '{record}': {host!r}"
    )
    parsed = urlparse(f"https://{host}")
    assert parsed.hostname == host.lower()
    assert parsed.port is None


def assert_https_url(url, field, record):
    assert isinstance(url, str) and url, f"{field} must be a non-empty string for '{record}'"
    parsed = urlparse(url)
    assert parsed.scheme == "https", f"{field} must use https for '{record}': {url}"
    assert parsed.hostname, f"{field} must have a host for '{record}': {url}"
    assert parsed.username is None and parsed.password is None, (
        f"{field} must not contain credentials for '{record}': {url}"
    )
    assert parsed.fragment == "", f"{field} must not contain a fragment for '{record}': {url}"
    assert parsed.port in (None, 443), f"{field} must use the default HTTPS port for '{record}': {url}"


def assert_optional_https_url(url, field, record):
    if url is not None:
        assert_https_url(url, field, record)


def _check_at_most_one_approved(catalog):
    approved = [
        source
        for source in catalog["sources"]
        if source["approval_state"] == "approved" and source["kind"] == "external"
    ]
    assert len(approved) <= 1, f"expected zero or one approved external source, got {len(approved)}"
    if approved:
        assert approved[0]["name"].lower() == catalog["mvp"]["source"].lower()


def _hosts_for(source, purpose):
    return set(source["ssrf"][f"{purpose}_hosts"])


def _check_source_ssrf_policy(catalog):
    policy = catalog["ssrf"]
    purposes = ("request", "evidence", "presentation")
    global_hosts = set()
    for purpose in purposes:
        hosts = policy[f"{purpose}_hosts"]
        assert isinstance(hosts, list) and hosts, f"global {purpose} host policy must be non-empty"
        for host in hosts:
            assert_bare_hostname(host, f"ssrf.{purpose}_hosts[]", "catalog")
        global_hosts.update(hosts)

    assert policy["require_https"] is True
    for source in catalog["sources"]:
        source_name = source["name"]
        source_hosts = {purpose: _hosts_for(source, purpose) for purpose in purposes}
        for purpose, hosts in source_hosts.items():
            assert hosts <= global_hosts, f"{source_name} {purpose} hosts escape global policy"
            for host in hosts:
                assert_bare_hostname(host, f"{source_name}.ssrf.{purpose}_hosts[]", source_name)

        assert urlparse(source["url"]).hostname in source_hosts["request"] | source_hosts["presentation"]
        for field, purpose in (("api.endpoint", "request"), ("api.docs", "evidence"), ("api.terms_url", "evidence")):
            section, key = field.split(".")
            url = source[section].get(key)
            if url:
                assert urlparse(url).hostname in source_hosts[purpose], (
                    f"{source_name} {field} host is not in its {purpose} SSRF policy"
                )
        for canonical_host in source["canonical_hosts"]:
            assert canonical_host in source_hosts["presentation"], (
                f"{source_name} canonical host is not a presentation host"
            )
        if source["approval_state"] == "approved":
            assert all(source_hosts[purpose] for purpose in purposes), (
                f"approved source {source_name} needs all typed SSRF policies"
            )


def _check_closed_world_permissions(catalog):
    for source in catalog["sources"]:
        pending_or_blocked = source["approval_state"] != "approved"
        if pending_or_blocked:
            assert source["allowed_matching_use"] == "none", source["name"]
            assert source["allowed_presentation_use"] == "none", source["name"]
            assert source["retention"] == "none", source["name"]
            assert source["deletion_expiry_obligations"] == [], source["name"]
            assert source["ingestion_policy"] == "none", source["name"]
            assert source["effective_permissions"] == {
                "matching": "none",
                "presentation": "none",
                "ingestion": False,
            }, source["name"]
        else:
            assert source["effective_permissions"]["ingestion"] is True


def _check_governance_types(catalog):
    assert isinstance(catalog["review"]["review_date"], dt.date)
    for key in ("reviewed_by", "decision_owner"):
        assert isinstance(catalog["review"][key], str) and catalog["review"][key].strip()
    for source in catalog["sources"]:
        name = source["name"]
        approval = source["approval"]
        assert isinstance(approval["owner"], str) and approval["owner"].strip()
        assert isinstance(approval["review_date"], dt.date), name
        assert isinstance(approval["evidence"], list), name
        assert isinstance(source["retention"], str) and source["retention"].strip(), name
        assert isinstance(source["deletion_expiry_obligations"], list), name
        assert isinstance(source["api"]["rate_limits"], dict), name
        assert isinstance(source["api"]["pagination"], dict), name
        assert isinstance(source["canonical_hosts"], list), name
        for host in source["canonical_hosts"]:
            assert_bare_hostname(host, "canonical_hosts[]", name)
        for field in ("compensation_fields", "location_fields", "matching_fields"):
            assert isinstance(source[field], list), f"{name}.{field}"


def _check_operational_controls(catalog):
    for source in catalog["sources"]:
        controls = source["operational_controls"]
        assert isinstance(controls["timeout_seconds"], int) and controls["timeout_seconds"] > 0
        assert isinstance(controls["max_response_size_bytes"], int) and controls["max_response_size_bytes"] > 0
        retry = controls["retry"]
        assert isinstance(retry["max_attempts"], int) and retry["max_attempts"] >= 1
        assert isinstance(retry["backoff_seconds"], (int, float)) and retry["backoff_seconds"] >= 0
        assert isinstance(retry["max_backoff_seconds"], (int, float))
        assert isinstance(retry["on_statuses"], list) and all(isinstance(status, int) for status in retry["on_statuses"])
        assert isinstance(controls["concurrency_limit"], int) and controls["concurrency_limit"] >= 1
        budget = controls["polling_budget"]
        assert isinstance(budget["max_requests_per_run"], int) and budget["max_requests_per_run"] >= 0
        assert isinstance(budget["min_interval_seconds"], (int, float)) and budget["min_interval_seconds"] >= 0
        assert isinstance(budget["status"], str) and budget["status"]
        assert isinstance(controls["failure_alert"], str) and controls["failure_alert"]


@pytest.fixture(scope="module")
def catalog():
    return load_yaml(CATALOG)


def test_catalog_parseable_and_shaped(catalog):
    assert catalog["review"].keys() >= REQUIRED_REVIEW_KEYS
    assert catalog["review"]["issue"] == 316
    assert catalog["mvp"]["source"]
    assert catalog["mvp"]["live_enabled"] is False
    assert catalog["live_enabled"] is False
    assert catalog["mvp"]["ssrf"]["request_hosts"]
    assert catalog["data_classification"]["approved_mvp"] == "none_pending_source_review"
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


def test_governance_fields_have_semantic_types(catalog):
    _check_governance_types(catalog)


def test_only_approved_sources_can_have_effective_permissions(catalog):
    _check_closed_world_permissions(catalog)


def test_operational_controls_are_source_specific(catalog):
    _check_operational_controls(catalog)


def test_at_most_one_approved_external_mvp_source(catalog):
    _check_at_most_one_approved(catalog)


def test_mvp_source_is_not_live_by_default(catalog):
    mvp_name = catalog["mvp"]["source"].lower()
    mvp_sources = [source for source in catalog["sources"] if source["name"].lower() == mvp_name]
    assert len(mvp_sources) == 1
    assert catalog["live_enabled"] is False
    assert catalog["mvp"]["live_enabled"] is False
    assert mvp_sources[0]["live_enabled"] is False


def test_ssrf_allowlist_contains_valid_typed_bare_hostnames(catalog):
    for purpose in ("request", "evidence", "presentation"):
        hosts = catalog["ssrf"][f"{purpose}_hosts"]
        assert hosts
        for host in hosts:
            assert_bare_hostname(host, f"ssrf.{purpose}_hosts[]", "catalog")


def test_source_ssrf_policy_covers_request_evidence_and_presentation_hosts(catalog):
    _check_source_ssrf_policy(catalog)


def test_fetch_url_guardrail_rejects_unsafe_forms():
    for value in (
        "http://example.com/",
        "https://user:pass@example.com/",
        "https://example.com/path#fragment",
        "https://example.com:8443/",
    ):
        with pytest.raises(AssertionError):
            assert_https_url(value, "url", "unsafe")
    assert_https_url("https://example.com:443/path", "url", "safe")


def test_approved_source_branches_with_mock(catalog):
    """Exercise approved-source branches without approving a real source."""
    mock = copy.deepcopy(catalog)
    first = mock["sources"][0]
    first["approval_state"] = "approved"
    first["approval"]["state"] = "approved"
    first["url"] = "https://example.com/"
    first["api"]["endpoint"] = "https://example.com/api"
    first["api"]["docs"] = "https://example.com/docs"
    first["api"]["terms_url"] = "https://example.com/terms"
    first["ssrf"] = {
        "request_hosts": ["example.com"],
        "evidence_hosts": ["example.com"],
        "presentation_hosts": ["example.com"],
    }
    first["canonical_hosts"] = ["example.com"]
    first["allowed_matching_use"] = "normalized_metadata_matching"
    first["allowed_presentation_use"] = "attributed_canonical_link"
    first["retention"] = "source_defined_short_retention"
    first["deletion_expiry_obligations"] = ["delete_on_source_expiry"]
    first["effective_permissions"] = {"matching": "normalized_metadata_matching", "presentation": "attributed_canonical_link", "ingestion": True}
    first["ingestion_policy"] = "fixture_first_after_source_approval"
    mock["mvp"]["source"] = first["name"]
    for purpose in ("request", "evidence", "presentation"):
        mock["ssrf"][f"{purpose}_hosts"].append("example.com")

    _check_at_most_one_approved(mock)
    _check_source_ssrf_policy(mock)
    _check_closed_world_permissions(mock)
    _check_governance_types(mock)
    _check_operational_controls(mock)
