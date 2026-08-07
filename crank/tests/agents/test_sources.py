# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from datetime import datetime, timezone as tz

import pytest
from django.test import TestCase

from crank.agents.sources.base import (
    SourceAdapter,
    SourceBlocked,
    SourceDisabled,
    SourceNotApproved,
    UnknownSourceAdapter,
    UnapprovedBaseUrl,
)
from crank.agents.sources.observation import (
    ObservationValidationError,
    RawScoreObservation,
)
from crank.agents.sources.registry import (
    REGISTRY,
    SourceRegistry,
    build_adapter,
    register_source_adapter,
    validate_observation_for_source,
)

from crank.models.organization import Organization
from crank.models.score import ScoreType
from crank.models.source import ApprovalState, SourceCatalog


class _FakeAdapter:
    key = "fake.v1"
    version = "1.0.0"

    def __init__(self, source):
        self.source = source

    def fetch(self, source):
        return []


def make_source(base_url="https://ratings.example.test", adapter_key="fake.v1",
                approval_state=ApprovalState.APPROVED, enabled=True):
    org = Organization.objects.create(name=f"org-{adapter_key}", gives_ratings=True, status=1)
    return SourceCatalog.objects.create(
        organization=org, name=f"src-{adapter_key}", adapter_key=adapter_key,
        base_url=base_url, approval_state=approval_state, enabled=enabled,
    )


class RegistryTests(TestCase):
    def test_register_get_and_contains(self):
        registry = SourceRegistry()
        registry.register(_FakeAdapter)
        self.assertIn("fake.v1", registry)
        self.assertEqual(registry.get("fake.v1"), _FakeAdapter)
        self.assertEqual(registry.keys(), ["fake.v1"])

    def test_register_rejects_missing_or_duplicate_key(self):
        class NoKey:
            pass

        registry = SourceRegistry()
        with pytest.raises(ValueError):
            registry.register(NoKey)  # type: ignore[arg-type]
        registry.register(_FakeAdapter)
        with pytest.raises(ValueError):
            registry.register(_FakeAdapter)

    def test_register_decorator_and_case_insensitive_lookup(self):
        @register_source_adapter
        class DecoratedAdapter:
            key = "decorated.v1"
            version = "1.0"

        self.assertIn("decorated.v1", REGISTRY)
        self.assertEqual(REGISTRY.get("DECORATED.V1"), DecoratedAdapter)


class BuildAdapterTests(TestCase):
    def setUp(self):
        self.registry = SourceRegistry()
        self.registry.register(_FakeAdapter)

    def test_approved_enabled_registered_builds(self):
        src = make_source(adapter_key="fake.v1")
        adapter = self.registry_get_adapter(src)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.key, "fake.v1")

    def test_rejects_unknown_adapter_key(self):
        src = make_source(adapter_key="no.such.v9")
        with pytest.raises(UnknownSourceAdapter):
            self.registry_get_adapter(src)

    def test_rejects_pending_source(self):
        src = make_source(adapter_key="fake.v1", approval_state=ApprovalState.PENDING)
        with pytest.raises(SourceNotApproved):
            self.registry_get_adapter(src)

    def test_rejects_blocked_source(self):
        src = make_source(adapter_key="fake.v1", approval_state=ApprovalState.BLOCKED)
        with pytest.raises(SourceBlocked):
            self.registry_get_adapter(src)

    def test_rejects_unknown_approval_value(self):
        src = make_source(adapter_key="fake.v1")
        src.approval_state = "anything-else"
        with pytest.raises(SourceNotApproved):
            self.registry_get_adapter(src)

    def test_rejects_disabled_but_approved(self):
        src = make_source(adapter_key="fake.v1", enabled=False)
        with pytest.raises(SourceDisabled):
            self.registry_get_adapter(src)

    def test_rejects_base_url_not_on_allowlist(self):
        src = make_source(adapter_key="fake.v1", base_url="https://evil.example.com")
        with pytest.raises(UnapprovedBaseUrl):
            self.registry_get_adapter(src)

    def test_accepts_subdomain_of_allowlisted_domain(self):
        src = make_source(adapter_key="fake.v1", base_url="https://api.ratings.example.test")
        self.assertIsNotNone(self.registry_get_adapter(src))

    def test_adapter_protocol_satisfied(self):
        src = make_source(adapter_key="fake.v1")
        adapter = self.registry_get_adapter(src)
        self.assertIsInstance(adapter, SourceAdapter)

    def registry_get_adapter(self, src):
        # route through the public build_adapter but using the per-test registry
        from crank.agents.sources import registry as reg_module
        original = reg_module.REGISTRY
        reg_module.REGISTRY = self.registry
        try:
            return build_adapter(src)
        finally:
            reg_module.REGISTRY = original


def observation(**overrides):
    base = dict(
        external_id="e-1",
        source_url="https://ratings.example.test/scores/e-1",
        target_identity="acme",
        score_type="culture",
        value=4.0,
        min_value=0.0,
        max_value=5.0,
        raw_value="4.0",
        observed_at=datetime(2026, 8, 1, 12, 0, tzinfo=tz.utc),
        fetched_at=datetime(2026, 8, 1, 12, 5, tzinfo=tz.utc),
        adapter_version="1.0.0",
        run_correlation="corr-1",
    )
    base.update(overrides)
    return base


class ObservationValidationTests(TestCase):
    def test_valid_observation_constructs(self):
        obs = RawScoreObservation(**observation())
        self.assertEqual(obs.external_id, "e-1")
        self.assertTrue(obs.run_correlation)

    def test_missing_required_field_rejected(self):
        payload = observation()
        del payload["target_identity"]
        with pytest.raises((ObservationValidationError, TypeError)):
            RawScoreObservation(**payload)

    def test_unknown_field_rejected(self):
        payload = observation()
        payload["surprise"] = 1
        with pytest.raises(TypeError):
            RawScoreObservation(**payload)

    def test_blank_required_string_rejected(self):
        payload = observation()
        payload["source_url"] = "   "
        with pytest.raises(ObservationValidationError):
            RawScoreObservation(**payload)

    def test_non_finite_value_rejected(self):
        for field in ("value", "min_value", "max_value"):
            payload = observation()
            payload[field] = float("nan")
            with pytest.raises(ObservationValidationError):
                RawScoreObservation(**payload)

    def test_inverted_range_rejected(self):
        payload = observation(min_value=10.0, max_value=1.0)
        with pytest.raises(ObservationValidationError):
            RawScoreObservation(**payload)

    def test_value_outside_range_rejected(self):
        payload = observation(value=11.0)
        with pytest.raises(ObservationValidationError):
            RawScoreObservation(**payload)

    def test_invalid_range_boundary_equal_rejected(self):
        payload = observation(min_value=5.0, max_value=5.0, value=5.0)
        with pytest.raises(ObservationValidationError):
            RawScoreObservation(**payload)

    def test_naive_timestamps_rejected(self):
        payload = observation(
            observed_at=datetime(2026, 8, 1, 12, 0)  # naive
        )
        with pytest.raises(ObservationValidationError):
            RawScoreObservation(**payload)

    def test_observed_after_fetched_rejected(self):
        payload = observation(
            observed_at=datetime(2026, 8, 1, 12, 10, tzinfo=tz.utc),
            fetched_at=datetime(2026, 8, 1, 12, 5, tzinfo=tz.utc),
        )
        with pytest.raises(ObservationValidationError):
            RawScoreObservation(**payload)

    def test_confidence_out_of_range_rejected(self):
        payload = observation(confidence=1.5)
        with pytest.raises(ObservationValidationError):
            RawScoreObservation(**payload)
        RawScoreObservation(**observation(confidence=0.5))

    def test_immutable(self):
        obs = RawScoreObservation(**observation())
        with pytest.raises(Exception):
            obs.external_id = "changed"  # type: ignore[misc]
        with pytest.raises(Exception):
            obs.value = 2.0  # type: ignore[misc]


class ObservationAgainstSourceTests(TestCase):
    def test_valid_observation_passes_source_validation(self):
        st = ScoreType.objects.create(name="culture")
        src = make_source(adapter_key="fake.v1")
        src.supported_score_types.add(st)
        obs = RawScoreObservation(**observation())
        validate_observation_for_source(obs, src)  # must not raise

    def test_unknown_score_type_rejected(self):
        src = make_source(adapter_key="fake.v1")
        obs = RawScoreObservation(**observation())
        with pytest.raises(ObservationValidationError):
            validate_observation_for_source(obs, src)

    def test_observation_source_url_policy_enforced(self):
        st = ScoreType.objects.create(name="culture")
        src = make_source(adapter_key="fake.v1")
        src.supported_score_types.add(st)
        obs = RawScoreObservation(**observation(source_url="https://evil.example.com/x"))
        with pytest.raises(ObservationValidationError):
            validate_observation_for_source(obs, src)

    def test_validate_source_base_url_malformed(self):
        from crank.agents.sources.base import validate_source_base_url, UnapprovedBaseUrl

        class BadURL(str):
            def strip(self, chars=None):
                return self
            def __getitem__(self, item):
                return self
            def split(self, *args, **kwargs):
                raise RuntimeError("split error")

        bad_url = BadURL("https://example.com/test")
        with pytest.raises(UnapprovedBaseUrl, match="Malformed source base URL"):
            validate_source_base_url(bad_url)

    def test_env_float_invalid(self):
        from unittest.mock import patch
        from crank.settings.base import _env_float
        with patch.dict("os.environ", {"TEST_FLOAT_ENV": "invalid_number"}):
            assert _env_float("TEST_FLOAT_ENV", 3.14) == 3.14

    def test_as_observation_helper(self):
        from crank.agents.sources.observation import as_observation
        obs = as_observation(observation())
        assert obs.external_id == "e-1"

    def test_observation_non_datetime_timestamp(self):
        with pytest.raises(ObservationValidationError):
            RawScoreObservation(**observation(observed_at=12345))

    def test_observation_nonfinite_numeric(self):
        with pytest.raises(ObservationValidationError):
            RawScoreObservation(**observation(value=float("inf")))

    def test_is_domain_allowed_empty(self):
        from crank.agents.sources.allowlist import is_domain_allowed
        assert not is_domain_allowed("")

    def test_registry_len(self):
        from crank.agents.sources.registry import REGISTRY
        assert isinstance(len(REGISTRY), int)

    def test_validate_source_base_url_userinfo(self):
        from crank.agents.sources.base import validate_source_base_url, UnapprovedBaseUrl
        with pytest.raises(UnapprovedBaseUrl, match="must not embed credentials"):
            validate_source_base_url("https://user:***@api.yelp.com/v3")

    def test_observation_nan_min_value(self):
        from crank.agents.sources.observation import RawScoreObservation, ObservationValidationError
        with pytest.raises(ObservationValidationError, match="must be a finite number"):
            RawScoreObservation(**observation(min_value=float("nan")))

    def test_observation_bool_numeric(self):
        from crank.agents.sources.observation import RawScoreObservation, ObservationValidationError
        with pytest.raises(ObservationValidationError, match="must be a finite number"):
            RawScoreObservation(**observation(value=True))
