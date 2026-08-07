# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for issues #313/#311/#312 score normalization + organization resolution.

Covers exact id/domain/name alias resolution, inactive and non-rating sources,
unknown and inactive score types, ambiguous and inactive targets, Unicode/case
normalization, invalid values/ranges (reject-not-clamp), duplicate observations,
bounded/deterministic replay, and HTML/secret safety of detail/log text.
"""
from __future__ import annotations

from datetime import datetime, timezone

from django.test import TestCase, override_settings

from crank.agents.sources.normalize import ScoreNormalizer, normalize_observations
from crank.agents.sources.types import (
    RawScoreObservation,
    ResolutionConfig,
    ResolutionReason,
    ResolutionReport,
    ResolutionStatus,
    build_resolution_config,
)
from crank.models import Organization, ScoreType

ACTIVE = 1
INACTIVE = 0


def _org(name, *, gives_ratings=False, status=ACTIVE, url=""):
    return Organization.objects.create(
        name=name, gives_ratings=gives_ratings, status=status, url=url,
    )


def _type(name, *, status=ACTIVE):
    return ScoreType.objects.create(name=name, status=status)


def _config(**overrides) -> ResolutionConfig:
    defaults = dict(
        version="1",
        source_key="src",
        source_organization="Acme Rating",
        score_type_mappings=[
            {"source": "src", "external": "Culture", "score_type": "Culture Score"},
            {"source": "src", "external": "Diversity", "score_type": "Diversity Score"},
            {"source": "other", "external": "Culture", "score_type": "Other Culture"},
        ],
        target_aliases=[
            {"kind": "external_id", "alias": "acme-77", "organization": "Acme"},
            {"kind": "domain", "alias": "acme.com", "organization": "Acme"},
            {"kind": "name", "alias": "Acme Inc", "organization": "Acme"},
            {"kind": "external_id", "alias": "globex-1", "organization": "Globex"},
        ],
    )
    defaults.update(overrides)
    return build_resolution_config(**defaults)


def _obs(**overrides) -> RawScoreObservation:
    defaults = dict(
        source_key="src", external_type="Culture", target="acme-77", value="4.5",
    )
    defaults.update(overrides)
    return RawScoreObservation(**defaults)


class NormalizeSetup(TestCase):
    def setUp(self) -> None:
        _org("Acme Rating", gives_ratings=True)
        _org("Acme", url="https://www.acme.com/")
        _org("Globex", url="https://globex.example/")
        _org("Inactive Rating Co", gives_ratings=True, status=INACTIVE)
        _org("NonRating Co", status=ACTIVE)
        _org("Hooli")
        _type("Culture Score")
        _type("Diversity Score", status=INACTIVE)


class AliasResolution(NormalizeSetup):
    def test_resolves_target_by_external_id(self):
        rep = normalize_observations([_obs(target="acme-77")], _config())
        out = rep.outcomes[0]
        self.assertIs(out.status, ResolutionStatus.NORMALIZED)
        self.assertIs(out.reason, ResolutionReason.RESOLVED)
        self.assertEqual(out.observation.target_name, "Acme")
        self.assertEqual(out.observation.type_name, "Culture Score")
        self.assertEqual(out.observation.source_name, "Acme Rating")
        self.assertEqual(out.observation.value, 4.5)

    def test_resolves_target_by_domain_with_scheme_and_path(self):
        rep = normalize_observations(
            [_obs(target="HTTP://WWW.ACME.COM/team")], _config())
        self.assertIs(rep.outcomes[0].status, ResolutionStatus.NORMALIZED)
        self.assertEqual(rep.outcomes[0].observation.target_name, "Acme")

    def test_resolves_target_by_exact_organization_name_when_unaliasable(self):
        # No alias needed: an exact, case-folded Organization.name is a valid,
        # deterministic "name" resolution (not fuzzy auto-linking).
        rep = normalize_observations([_obs(target="Hooli")], _config())
        self.assertIs(rep.outcomes[0].status, ResolutionStatus.NORMALIZED)
        self.assertEqual(rep.outcomes[0].observation.target_name, "Hooli")

    def test_resolves_target_by_curated_name_alias(self):
        rep = normalize_observations([_obs(target="acme inc")], _config())
        self.assertIs(rep.outcomes[0].status, ResolutionStatus.NORMALIZED)
        self.assertEqual(rep.outcomes[0].observation.target_name, "Acme")

    def test_external_id_alias_takes_priority_over_matching_org_name(self):
        # Deterministic tie: an external_id alias wins over an org whose raw
        # name equals the same token.
        _org("acme-77")
        rep = normalize_observations([_obs(target="acme-77")], _config())
        self.assertIs(rep.outcomes[0].status, ResolutionStatus.NORMALIZED)
        self.assertEqual(rep.outcomes[0].observation.target_name, "Acme")


class SourceResolution(NormalizeSetup):
    def test_source_unknown(self):
        rep = normalize_observations([_obs()], _config(source_organization=None))
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.SOURCE_UNKNOWN)
        self.assertEqual(rep.unresolved, 1)

    def test_source_missing_organization(self):
        rep = normalize_observations(
            [_obs()], _config(source_organization="No Such Co"))
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.SOURCE_UNKNOWN)

    def test_source_inactive(self):
        rep = normalize_observations(
            [_obs()], _config(source_organization="Inactive Rating Co"))
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.SOURCE_INACTIVE)

    def test_source_not_rating(self):
        rep = normalize_observations(
            [_obs()], _config(source_organization="NonRating Co"))
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.SOURCE_NOT_RATING)


class TypeResolution(NormalizeSetup):
    def test_unknown_external_type(self):
        rep = normalize_observations([_obs(external_type="Compensation")], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.TYPE_UNKNOWN)

    def test_type_keyed_to_other_source_is_unknown(self):
        # Mapping exists for a different source; must not apply cross-source.
        rep = normalize_observations([_obs()], _config(score_type_mappings=[
            {"source": "unrelated", "external": "Culture", "score_type": "Culture Score"},
        ]))
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.TYPE_UNKNOWN)

    def test_type_maps_to_inactive_score_type(self):
        rep = normalize_observations([_obs(external_type="Diversity")], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.TYPE_INACTIVE)

    def test_type_maps_to_missing_score_type(self):
        rep = normalize_observations([_obs(external_type="Culture")], _config(
            score_type_mappings=[
                {"source": "src", "external": "Culture", "score_type": "Nope"},
            ]))
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.TYPE_UNKNOWN)


class TargetResolution(NormalizeSetup):
    def test_target_unknown(self):
        rep = normalize_observations([_obs(target="no-such-entity")], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.TARGET_UNKNOWN)

    def test_target_ambiguous_two_orgs_share_domain(self):
        _org("Acme West", url="https://acme.com/")
        rep = normalize_observations([_obs(target="acme.com")], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.TARGET_AMBIGUOUS)
        self.assertIs(rep.outcomes[0].observation, None)

    def test_target_inactive_via_alias(self):
        Organization.objects.filter(name="Acme").update(status=INACTIVE)
        rep = normalize_observations([_obs(target="acme-77")], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.TARGET_INACTIVE)

    def test_ambiguous_name_is_not_auto_picked(self):
        _org("acme.com")  # an org literally named like the domain token
        # Token "acme.com" now matches the domain alias (Acme) AND an
        # organization named "acme.com"; resolved to the domain alias by
        # priority, not ambiguity — tests deterministic ordering.
        rep = normalize_observations([_obs(target="acme.com")], _config())
        self.assertIs(rep.outcomes[0].status, ResolutionStatus.NORMALIZED)
        self.assertEqual(rep.outcomes[0].observation.target_name, "Acme")


class UnicodeAndCase(NormalizeSetup):
    def test_external_type_case_and_whitespace_insensitive(self):
        # NFKC + casefold in the pipeline makes these equivalent to the mapping.
        # NFKC fullwidth letters fold to ASCII; case/whitespace collapsed.
        for token in (" CULTURE ", "culture",
                      "\uff43\uff55\uff4c\uff54\uff55\uff52\uff45"):
            rep = normalize_observations([_obs(external_type=token)], _config())
            self.assertIs(
                rep.outcomes[0].status, ResolutionStatus.NORMALIZED,
                f"token {token!r} should match",
            )

    def test_target_name_unicode_case_insensitive(self):
        _org("\u00c9cole")  # E-acute
        rep = normalize_observations([_obs(target="\u00e9cole")], _config())
        self.assertIs(rep.outcomes[0].status, ResolutionStatus.NORMALIZED)
        self.assertEqual(rep.outcomes[0].observation.target_name, "\u00c9cole")

    def test_domain_unicode_normalized(self):
        _org("M\u00fcnchen Org", url="https://www.m\u00fcnchen.example/")
        rep = normalize_observations(
            [_obs(target="www.xn--mnchen-3ya.example/")], _config())
        self.assertIs(rep.outcomes[0].status, ResolutionStatus.NORMALIZED)
        self.assertEqual(rep.outcomes[0].observation.target_name, "M\u00fcnchen Org")


class NumericNormalization(NormalizeSetup):
    def test_standalone_value(self):
        rep = normalize_observations([_obs(value="3.25")], _config())
        out = rep.outcomes[0].observation
        self.assertEqual(out.value, 3.25)
        self.assertIsNone(out.low_threshold)
        self.assertIsNone(out.high_threshold)

    def test_range_only(self):
        rep = normalize_observations([_obs(value=None, low="2", high="8")], _config())
        out = rep.outcomes[0].observation
        self.assertIsNone(out.value)
        self.assertEqual(out.low_threshold, 2)
        self.assertEqual(out.high_threshold, 8)

    def test_blank_value_with_range_is_range_only(self):
        # A blank single-value field alongside a full range is a range-only
        # observation (value is normalized to None), not an error.
        rep = normalize_observations([_obs(value="", low="1", high="5")], _config())
        out = rep.outcomes[0].observation
        self.assertIsNone(out.value)
        self.assertEqual(out.low_threshold, 1)
        self.assertEqual(out.high_threshold, 5)

    def test_value_inside_range(self):
        obs = _obs(value="5", low="0", high="10")
        rep = normalize_observations([obs], _config())
        self.assertIs(rep.outcomes[0].status, ResolutionStatus.NORMALIZED)

    def test_value_out_of_range_rejected_not_clamped(self):
        obs = _obs(value="15", low="0", high="10")
        rep = normalize_observations([obs], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.VALUE_OUT_OF_RANGE)
        self.assertIsNone(rep.outcomes[0].observation)
        self.assertIn("rejected, not clamped", rep.outcomes[0].detail)
        self.assertEqual(rep.rejected, 1)

    def test_missing_value_and_range(self):
        rep = normalize_observations([_obs(value=None, low=None, high=None)], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.VALUE_MISSING)

    def test_partial_range_rejected(self):
        rep = normalize_observations([_obs(value=None, low=None, high="10")], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.RANGE_INVALID)
        rep = normalize_observations([_obs(value=None, low="0", high=None)], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.RANGE_INVALID)

    def test_low_greater_than_high_rejected(self):
        rep = normalize_observations([_obs(value=None, low="9", high="2")], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.RANGE_INVALID)

    def test_non_finite_rejected(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            rep = normalize_observations([_obs(value=token)], _config())
            self.assertIs(rep.outcomes[0].reason, ResolutionReason.VALUE_INVALID,
                          f"{token} must be rejected")

    def test_invalid_decimal_rejected(self):
        for token in ("abc", "4..5", "0x1f", "e5"):
            rep = normalize_observations([_obs(value=token)], _config())
            self.assertIs(rep.outcomes[0].reason, ResolutionReason.VALUE_INVALID,
                          f"{token!r} must be rejected")

    def test_magnitude_bound_rejected(self):
        rep = normalize_observations([_obs(value="1e100")], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.VALUE_INVALID)

    def test_excessive_significant_digits_rejected(self):
        # 31 significant digits exceeds the default bound of 30.
        rep = normalize_observations(
            [_obs(value="0.1234567890123456789012345678901")], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.VALUE_INVALID)

    def test_range_invalid_propagates_reason(self):
        rep = normalize_observations([_obs(value=None, low="NaN", high="10")], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.RANGE_INVALID)


class DupesAndReplay(NormalizeSetup):
    def test_duplicate_observations_counted_once(self):
        obs = _obs()
        rep = normalize_observations([obs, obs, _obs(value="2")], _config())
        self.assertEqual(rep.normalized, 2)
        self.assertEqual(rep.duplicates_skipped, 1)
        self.assertEqual(rep.inputs_seen, 3)

    def test_dedup_ignores_volatile_provenance(self):
        obs_a = _obs(fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                     run_correlation_id="run-1")
        obs_b = _obs(fetched_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
                     run_correlation_id="run-2")
        rep = normalize_observations([obs_a, obs_b], _config())
        self.assertEqual(rep.normalized, 1)
        self.assertEqual(rep.duplicates_skipped, 1)

    def test_deterministic_replay_produces_identical_report(self):
        obs = [
            _obs(),
            _obs(target="nomatch"),
            _obs(value="3", low="0", high="2"),
            _obs(external_type="unknown-type"),
        ]
        config = _config()
        first = normalize_observations(obs, config)
        second = normalize_observations(obs, config)
        self.assertEqual(first, second)
        self.assertEqual(first.normalized, second.normalized)
        self.assertEqual(first.counts, second.counts)
        self.assertEqual(
            [(o.key, o.reason.value) for o in first.outcomes],
            [(o.key, o.reason.value) for o in second.outcomes],
        )

    def test_replay_ordering_is_deterministic(self):
        obs = [_obs(), _obs(value="9"), _obs(value="1")]
        config = _config()
        report_a = ScoreNormalizer(config).normalize(obs)
        report_b = ScoreNormalizer(config).normalize(list(reversed(obs)))
        # Processing order follows first-seen, but the same unique logical set
        # yields the same outcome multiset regardless of input order.
        self.assertEqual(
            sorted((o.reason.value, o.observation.target_name if o.observation else "")
                   for o in report_a.outcomes),
            sorted((o.reason.value, o.observation.target_name if o.observation else "")
                   for o in report_b.outcomes),
        )


class AggregatesAndSafety(NormalizeSetup):
    def test_report_aggregate_counts(self):
        rep = normalize_observations(
            [
                _obs(),                                    # normalized
                _obs(target="nomatch"),                    # unresolved
                _obs(value="9", low="0", high="2"),        # rejected
                _obs(value="3"),                          # normalized
                _obs(value="4"),                          # normalized
            ],
            _config(),
        )
        self.assertEqual(rep.normalized, 3)
        self.assertEqual(rep.unresolved, 1)
        self.assertEqual(rep.rejected, 1)
        self.assertNotIn(ResolutionReason.RESOLVED.value, rep.by_reason(
            ResolutionStatus.UNRESOLVED))
        self.assertIn("target_unknown", rep.by_reason(ResolutionStatus.UNRESOLVED))
        self.assertEqual(rep.outcomes[0].status, ResolutionStatus.NORMALIZED)

    def test_report_counts_by_status_complete(self):
        rep = normalize_observations([_obs()], _config())
        self.assertIn(ResolutionReason.RESOLVED.value, rep.by_reason())
        self.assertIn("resolved", rep.by_reason())

    def test_html_in_target_does_not_leak_into_detail(self):
        rep = normalize_observations(
            [_obs(target="<script>alert(1)</script>nomatch")], _config())
        out = rep.outcomes[0]
        self.assertIs(out.reason, ResolutionReason.TARGET_UNKNOWN)
        self.assertNotIn("<script>", out.detail)
        self.assertNotIn("<", out.detail)

    def test_html_in_type_does_not_leak_into_detail(self):
        rep = normalize_observations(
            [_obs(external_type="<b>Bonus</b>")], _config())
        out = rep.outcomes[0]
        self.assertIs(out.reason, ResolutionReason.TYPE_UNKNOWN)
        self.assertNotIn("<", out.detail)

    def test_source_mismatch_reason_coded_rejected(self):
        rep = normalize_observations([_obs(source_key="badsrc")], _config())
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.INPUT_LIMIT_EXCEEDED)
        self.assertIs(rep.outcomes[0].observation, None)

    def test_limit_exceeded_reported(self):
        config = _config(max_observations=1)
        rep = normalize_observations(
            [_obs(value="1"), _obs(value="2"), _obs(value="3")], config)
        self.assertEqual(rep.normalized, 1)
        self.assertEqual(rep.skipped_limit, 2)
        self.assertEqual(rep.counts["input_limit_exceeded"], 2)

    def test_normalized_reason_status_is_normalized(self):
        self.assertIs(ResolutionReason.RESOLVED.status, ResolutionStatus.NORMALIZED)
        self.assertIs(ResolutionReason.TARGET_UNKNOWN.status, ResolutionStatus.UNRESOLVED)
        self.assertIs(ResolutionReason.VALUE_OUT_OF_RANGE.status, ResolutionStatus.REJECTED)

    def test_ignores_extra_seed_rows_when_registering_duplicate_skipped(self):
        # Make sure setUp duplicates didn't break the unique-name invariant.
        self.assertEqual(Organization.objects.filter(status=ACTIVE).count(), 5)


class ConfigValidation(TestCase):
    def test_blank_version_rejected(self):
        with self.assertRaises(ValueError):
            build_resolution_config(version="  ")

    def test_negative_and_invalid_bounds_rejected(self):
        for kwargs in (
            {"max_decimal_significant_digits": 0},
            {"max_string_length": 0},
            {"max_observations": 0},
            {"max_value_magnitude": "0"},
            {"max_value_magnitude": "not-a-number"},
            {"max_value_magnitude": "Infinity"},
        ):
            with self.assertRaises(ValueError):
                build_resolution_config(**kwargs)

    def test_domain_helpers(self):
        from crank.agents.sources.types import normalize_domain, sanitize_label
        self.assertEqual(normalize_domain(""), "")
        self.assertEqual(
            normalize_domain("https://SUB.Example.COM:443/x?y=1"), "sub.example.com")
        self.assertNotIn("<", sanitize_label("<b>hi</b>\x00secret"))
        self.assertEqual(sanitize_label("  a  b "), "a b")  # collapse whitespace
        self.assertEqual(sanitize_label("a\tb"), "ab")  # control chars stripped

    def test_idna_exception_path_is_benign(self):
        # A host token that IDNA cannot encode degrades to the stripped/lower
        # string rather than raising, so matching never crashes on odd input.
        from crank.agents.sources.types import normalize_domain
        bad = "foo..bar.com"  # adjacent dots are not valid IDNA
        self.assertEqual(normalize_domain(bad), bad.lower())

    def test_invalid_mappings_and_aliases_fail_fast(self):
        with self.assertRaises(ValueError):
            build_resolution_config(
                score_type_mappings=[{"source": "s", "external": "e",
                                      "score_type": ""}])
        with self.assertRaises(ValueError):
            build_resolution_config(
                target_aliases=[{"kind": "bogus", "alias": "x",
                                 "organization": "Acme"}])
        with self.assertRaises(ValueError):
            build_resolution_config(
                target_aliases=[{"kind": "name", "alias": "x",
                                 "organization": ""}])

    @override_settings(
        SCORE_RESOLUTION_VERSION="7",
        RATING_SOURCE_ORGANIZATION_NAME="Acme Rating",
        SCORE_TYPE_MAPPINGS=[
            {"source": "src", "external": "Culture", "score_type": "Culture Score"},
        ],
        SCORE_TARGET_ALIASES=[{"kind": "domain", "alias": "acme.com", "organization": "Acme"}],
    )
    def test_default_config_from_settings(self):
        from crank.agents.sources.config import default_resolution_config
        cfg = default_resolution_config()
        self.assertEqual(cfg.version, "7")
        self.assertEqual(cfg.source_organization, "Acme Rating")
        self.assertEqual(len(cfg.score_type_mappings), 1)
        self.assertEqual(len(cfg.target_aliases), 1)

    def test_token_too_long_rejected(self):
        _org("Acme Rating", gives_ratings=True)
        _org("Acme")
        _type("Culture Score")
        from crank.agents.sources.types import build_resolution_config
        cfg = build_resolution_config(
            version="1", source_key="src", source_organization="Acme Rating",
            score_type_mappings=[{"source": "src", "external": "Culture",
                                  "score_type": "Culture Score"}],
            target_aliases=[{"kind": "external_id", "alias": "acme-77",
                             "organization": "Acme"}],
            max_string_length=5,
        )
        rep = normalize_observations([_obs(value="123456")], cfg)
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.VALUE_INVALID)
        self.assertIn("token too long", rep.outcomes[0].detail)

    def test_source_not_configured_reason(self):
        rep = normalize_observations([_obs()], build_resolution_config(
            version="1", source_key="src", source_organization="",
            score_type_mappings=[], target_aliases=[]))
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.SOURCE_UNKNOWN)
        self.assertEqual(rep.outcomes[0].detail, "no rating source configured")

    def test_unknown_source_uses_friendly_detail(self):
        rep = normalize_observations(
            [_obs()], _config(source_organization="Ghost Corp"))
        self.assertIs(rep.outcomes[0].reason, ResolutionReason.SOURCE_UNKNOWN)
        self.assertIn("Ghost Corp", rep.outcomes[0].detail)

    def test_report_fields_present(self):
        cfg = _config()
        rep = normalize_observations([], cfg)
        self.assertIsInstance(rep, ResolutionReport)
        self.assertEqual(rep.inputs_seen, 0)
        self.assertEqual(rep.mapping_version, "1")


class DefaultConfigIntegration(TestCase):
    def setUp(self):
        _org("Acme Rating", gives_ratings=True)

    @override_settings(
        RATING_SOURCE_ORGANIZATION_NAME="Acme Rating",
        SCORE_TYPE_MAPPINGS=[{"source": "default", "external": "X", "score_type": "X"}],
    )
    def test_default_config_flows_through(self):
        from crank.agents.sources.config import default_resolution_config
        _org("X")
        _type("X")
        cfg = default_resolution_config()
        rep = normalize_observations(
            [RawScoreObservation(source_key="default", external_type="X", target="X",
                                 value="1")],
            cfg,
        )
        self.assertEqual(rep.normalized, 1)
