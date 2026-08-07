# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the typed raw-score observation contract."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crank.agents.sources.contract import RawScoreObservation
from crank.agents.sources.errors import SchemaDriftError

OBSERVED = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def make(**overrides):
    kwargs = {
        "external_id": "abc-123",
        "source_url": "https://www.yelp.com/biz/x",
        "target_identity": "Fixture Corp",
        "score_type": "rating",
        "value": 4.1,
        "range_low": 0.0,
        "range_high": 5.0,
        "observed_at": OBSERVED,
        "fetched_at": FETCHED,
        "adapter": "yelp",
        "adapter_version": "1.0.0",
        "run_correlation_id": "run-1",
    }
    kwargs.update(overrides)
    return RawScoreObservation.create(**kwargs)


class TestHappyPath:
    def test_creates_observation(self):
        obs = make()
        assert obs.external_id == "abc-123"
        assert obs.value == 4.1
        assert obs.adapter_version == "1.0.0"
        assert obs.run_correlation_id == "run-1"
        assert obs.observed_at.tzinfo is not None

    def test_run_correlation_id_optional(self):
        obs = make(run_correlation_id=None)
        assert obs.run_correlation_id is None


class TestValidation:
    def test_missing_external_id_rejected(self):
        with pytest.raises(SchemaDriftError):
            make(external_id="")

    def test_missing_url_rejected(self):
        with pytest.raises(SchemaDriftError):
            make(source_url=None)

    def test_missing_name_rejected(self):
        with pytest.raises(SchemaDriftError):
            make(target_identity="")

    def test_empty_run_correlation_rejected(self):
        with pytest.raises(SchemaDriftError):
            make(run_correlation_id="   ")

    def test_inverted_range_rejected(self):
        with pytest.raises(SchemaDriftError):
            make(range_low=5.0, range_high=1.0)

    def test_value_outside_range_rejected(self):
        with pytest.raises(SchemaDriftError):
            make(value=6.0, range_low=0.0, range_high=5.0)

    def test_value_below_range_rejected(self):
        with pytest.raises(SchemaDriftError):
            make(value=-0.5, range_low=0.0, range_high=5.0)

    def test_nonfinite_value_rejected(self):
        with pytest.raises(SchemaDriftError):
            make(value=float("inf"))

    def test_nonfinite_range_rejected(self):
        with pytest.raises(SchemaDriftError):
            make(range_high=float("nan"))

    def test_naive_timestamp_rejected(self):
        naive = datetime(2026, 8, 1, 12, 0)
        with pytest.raises(SchemaDriftError):
            make(observed_at=naive)

    def test_naive_fetched_rejected(self):
        naive = datetime(2026, 8, 1, 12, 0)
        with pytest.raises(SchemaDriftError):
            make(fetched_at=naive)

    def test_string_value_rejected_not_number(self):
        with pytest.raises(SchemaDriftError):
            make(value="4.1")


class TestImmutability:
    def test_unknown_field_rejected(self):
        with pytest.raises(TypeError):
            RawScoreObservation.create(
                external_id="a",
                source_url="b",
                target_identity="c",
                score_type="rating",
                value=1.0,
                range_low=0.0,
                range_high=5.0,
                observed_at=OBSERVED,
                fetched_at=FETCHED,
                adapter="x",
                adapter_version="1.0.0",
                bogus_field=1,
            )

    def test_object_is_frozen(self):
        obs = make()
        with pytest.raises(Exception):
            obs.value = 999  # type: ignore[misc]


    def test_require_aware_not_datetime(self):
        with pytest.raises(SchemaDriftError):
            make(observed_at="not-a-datetime")

    def test_observation_to_dict(self):
        from crank.agents.sources.contract import observation_to_dict
        obs = make()
        d = observation_to_dict(obs)
        assert d["external_id"] == "abc-123"
        assert d["observed_at"] == OBSERVED.isoformat()
