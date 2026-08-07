# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Guardrails for the machine-readable external rating source catalog.

Checks that docs/source-catalog.yaml (the #310 governance deliverable) stays
in sync with seeds/crank.organization.yaml and keeps its documented invariants:

- every seeded rating organization (gives_ratings=True) is catalogued;
- each source record is well-formed (urls parse, approval states are valid);
- exactly one external source is approved as the MVP source;
- the MVP source is never live by default and has an SSRF allowlist + data class.
"""

from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "docs" / "source-catalog.yaml"
ORGANIZATION_SEED = REPO_ROOT / "seeds" / "crank.organization.yaml"

ALLOWED_APPROVAL_STATES = {"approved", "pending", "blocked", "excluded"}
ALLOWED_KINDS = {"external", "internal"}
REQUIRED_SOURCE_KEYS = {
    "name",
    "pk",
    "url",
    "kind",
    "seed_gives_ratings",
    "score_types_candidates",
    "api",
    "approval",
}


def load_yaml(path):
    assert path.exists(), f"missing catalog source: {path}"
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def assert_http_url(url, field, record):
    assert isinstance(url, str) and url, f"{field} must be a non-empty string for '{record}'"
    parsed = urlparse(url)
    assert parsed.scheme in {"http", "https"}, f"{field} must be http(s) for '{record}': {url}"
    assert parsed.netloc, f"{field} must have a host for '{record}': {url}"


@pytest.fixture(scope="module")
def catalog():
    return load_yaml(CATALOG)


@pytest.fixture(scope="module")
def seeded_rating_names():
    seed = load_yaml(ORGANIZATION_SEED)
    names = {
        entry["fields"]["name"].lower()
        for entry in seed
        if entry.get("fields", {}).get("gives_ratings") is True
    }
    assert names, "expected at least one seeded rating organization"
    return names


def test_catalog_parseable_and_shaped(catalog):
    assert "review" in catalog
    assert catalog["review"]["review_date"]
    assert catalog["review"]["decision_owner"]
    assert "mvp" in catalog
    assert catalog["mvp"]["source"]
    assert "ssrf_allowlist" in catalog["mvp"]
    assert catalog["mvp"]["data_classification"]
    assert catalog["mvp"]["live_enabled"] is False
    assert isinstance(catalog["sources"], list) and len(catalog["sources"]) >= 1


def test_all_seeded_rating_sources_are_catalogued(catalog, seeded_rating_names):
    catalog_names = {s["name"].lower() for s in catalog["sources"]}
    missing = sorted(seeded_rating_names - catalog_names)
    assert not missing, f"seeded rating sources missing from catalog: {missing}"


def test_source_records_are_well_formed(catalog):
    pks = []
    for source in catalog["sources"]:
        name = source["name"]
        for key in REQUIRED_SOURCE_KEYS:
            assert key in source, f"{name} is missing required key '{key}'"
        assert_http_url(source["url"], "url", name)
        assert source["pk"] not in pks, f"duplicate pk {source['pk']}"
        pks.append(source["pk"])
        assert source["kind"] in ALLOWED_KINDS, f"{name} has invalid kind"
        assert isinstance(source["seed_gives_ratings"], bool)
        assert isinstance(source["score_types_candidates"], list)

        state = source["approval"]["state"]
        assert state in ALLOWED_APPROVAL_STATES, f"{name} has invalid approval state '{state}'"
        assert source["approval"]["owner"]
        assert source["approval"]["review_date"]

        evidence = source["approval"]["evidence"]
        if evidence:
            for url in evidence:
                assert_http_url(url, "approval.evidence[]", name)

        for field in ("terms_url", "docs"):
            value = source["api"].get(field)
            if value:
                assert_http_url(value, f"api.{field}", name)


def test_exactly_one_approved_external_mvp_source(catalog):
    approved = [
        s
        for s in catalog["sources"]
        if s["approval"]["state"] == "approved" and s["kind"] == "external"
    ]
    assert len(approved) == 1, f"expected exactly one approved external source, got {len(approved)}"
    mvp = catalog["mvp"]["source"].lower()
    assert approved[0]["name"].lower() == mvp, "mvp.source must match the approved external source"


def test_approved_source_is_not_live_before_keys(catalog):
    approved = [s for s in catalog["sources"] if s["approval"]["state"] == "approved"]
    for source in approved:
        assert source["approval"]["evidence"], "approved source must cite evidence"
        # The catalog-wide default is live_enabled: false (no credentials committed).
        assert catalog["mvp"]["live_enabled"] is False


def test_ssrf_allowlist_contains_valid_bare_hostnames(catalog):
    allowlist = catalog["mvp"]["ssrf_allowlist"]
    assert allowlist, "ssrf_allowlist must not be empty for the approved MVP source"
    for host in allowlist:
        assert isinstance(host, str) and host, "SSRF allowlist entries must be non-empty"
        assert "://" not in host, f"SSRF allowlist must be bare hostnames, got '{host}'"
        assert "/" not in host, f"SSRF allowlist must be hostnames (no path), got '{host}'"
        assert "." in host, f"SSRF allowlist entry must be a full hostname, got '{host}'"
