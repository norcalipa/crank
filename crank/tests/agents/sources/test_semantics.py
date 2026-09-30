# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Measurement semantics: which score types a source's numbers may feed (#474)."""

import pytest

from crank.agents.sources.registry import REGISTRY, SourceRegistry
from crank.agents.sources.semantics import (
    SCORE_TYPE_ALLOWED_MEASUREMENTS,
    MeasurementKind,
    coerce_kind,
    measurement_allows,
)

SEEDED_TYPES = [
    "Culture",
    "Total Compensation",
    "Leadership",
    "Reputation",
    "Product and Mission",
    "Tech Stack",
    "Lifecycle and Financials",
    "RTO and Work Hours",
]


def test_map_covers_exactly_the_seeded_types():
    assert set(SCORE_TYPE_ALLOWED_MEASUREMENTS) == set(SEEDED_TYPES)


@pytest.mark.parametrize("kind", list(MeasurementKind))
@pytest.mark.parametrize("name", SEEDED_TYPES)
def test_matrix_matches_the_code_map(kind, name):
    expected = kind in SCORE_TYPE_ALLOWED_MEASUREMENTS[name]
    assert measurement_allows(kind, name) is expected
    assert measurement_allows(kind.value, name) is expected


def test_consumer_business_rating_feeds_only_reputation():
    allowed = [
        name
        for name in SEEDED_TYPES
        if measurement_allows(MeasurementKind.CONSUMER_BUSINESS_RATING, name)
    ]
    assert allowed == ["Reputation"]


@pytest.mark.parametrize("kind", list(MeasurementKind))
@pytest.mark.parametrize("name", ["Hiring probability", "hiring Likelihood", "Unlisted", ""])
def test_unknown_or_hiring_types_accept_nothing(kind, name):
    assert measurement_allows(kind, name) is False


def test_invalid_kind_is_rejected():
    assert coerce_kind("star_rating") is None
    assert coerce_kind(None) is None
    assert measurement_allows("star_rating", "Reputation") is False


def test_registration_requires_a_valid_measurement_kind():
    class NoKind:
        key = "nokind.v1"

    class BadKind:
        key = "badkind.v1"
        measurement_kind = "star_rating"

    class Good:
        key = "good.v1"
        measurement_kind = MeasurementKind.CURATED_REVIEW

    registry = SourceRegistry()
    for cls in (NoKind, BadKind):
        with pytest.raises(ValueError, match="measurement_kind"):
            registry.register(cls)
    registry.register(Good)
    assert "good.v1" in registry


def test_production_registry_registers_no_blocked_source_adapter():
    import re
    from pathlib import Path

    import yaml

    import crank.agents.sources  # noqa: F401 - imports every adapter module

    catalog = yaml.safe_load(
        (Path(__file__).resolve().parents[4] / "docs" / "source-catalog.yaml").read_text()
    )
    blocked = {
        re.sub(r"[^a-z0-9]", "", source["name"].lower())
        for source in catalog["sources"]
        if source["approval"]["state"] in ("blocked", "pending")
    }
    assert "yelp" in blocked  # the derivation must see the catalog's blocked sources
    for key in REGISTRY.keys():
        stem = re.sub(r"[^a-z0-9]", "", key.split(".")[0].lower())
        assert stem not in blocked, f"adapter {key!r} is registered for a blocked/pending source"


def test_lifecycle_and_financials_does_not_accept_compensation_benchmarks():
    from crank.agents.sources.semantics import MeasurementKind, measurement_allows

    assert not measurement_allows(MeasurementKind.COMPENSATION_BENCHMARK, "Lifecycle and Financials")
    assert measurement_allows(MeasurementKind.FINANCIAL_DATA, "Lifecycle and Financials")
    assert measurement_allows(MeasurementKind.COMPENSATION_BENCHMARK, "Total Compensation")
