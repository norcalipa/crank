# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Deterministic score normalization and organization resolution pipeline.

Given a :class:`~crank.agents.sources.types.ResolutionConfig` and a set of raw
external observations, this module:

1. Resolves the configured rating source to an *active* ``Organization`` whose
   ``gives_ratings=True``.
2. Maps each external score type through the curated source-specific mappings
   to an *active* ``ScoreType``.
3. Resolves each target through curated external-id/domain/name aliases against
   *active* organizations (never auto-creating).
4. Normalizes values/ranges with ``Decimal``-safe, reject-not-clamp rules.
5. Emits a typed normalized observation for every success plus a reason-coded
   outcome and aggregate counts for every unresolved/rejected observation.

Identities are resolved in a documented, fixed order so the pipeline is
deterministic: the same ``config.version`` plus the same inputs always produce
the same outcomes and counts. Ambiguity at any resolution step is reported and
never auto-picked, and no score is persisted and nothing is scheduled here.
"""
import logging
from decimal import Decimal, InvalidOperation
from typing import Iterable, List, Optional, Sequence

from crank.agents.sources.types import (
    NormalizedScoreObservation,
    ObservationOutcome,
    RawScoreObservation,
    ResolutionConfig,
    ResolutionReason,
    ResolutionReport,
    ResolutionStatus,
    TargetAliasKind,
    normalize_domain,
    normalize_token,
    observation_key,
    sanitize_label,
)

logger = logging.getLogger("crank.agents.sources.normalize")

ACTIVE_STATUS = 1


class _InvalidNumber(ValueError):
    """A numeric token failed normalization with an exact reason code."""

    def __init__(self, reason: ResolutionReason, detail: str = ""):
        self.reason = reason
        super().__init__(detail or reason.value)


class ScoreNormalizer:
    """Resolves and normalizes raw observations against a fixed config.

    The class caches the resolved identity index (active organizations, active
    score types, curated aliases) computed once per construction so a batch of
    observations is processed deterministically against a single snapshot.
    Outside callers should generally use :func:`normalize_observations`, which
    constructs one normalizer per pass.
    """

    def __init__(self, config: ResolutionConfig, *, organization_model=None,
                 score_type_model=None):
        from crank.models import Organization, ScoreType  # deferred import
        self.config = config
        self._org_model = organization_model or Organization
        self._type_model = score_type_model or ScoreType
        self._active_orgs = {}  # name -> model instance (active only)
        self._active_types = {}  # name -> model instance (active only)
        self._alias_index = {}  # (kind, normalized token) -> set of org names
        self._source = None  # resolved rating org, else (reason, detail)
        self._load_active_rows()
        self._index_aliases()
        self._resolve_source()

    # -- snapshot loading ----------------------------------------------------

    def _load_active_rows(self):
        for org in self._org_model.objects.filter(status=ACTIVE_STATUS).order_by("id"):
            self._active_orgs[org.name] = org
        for st in self._type_model.objects.filter(status=ACTIVE_STATUS).order_by("id"):
            self._active_types[st.name] = st

    def _index_aliases(self):
        for alias in self.config.target_aliases:
            normalized = self._alias_token(alias.kind, alias.alias)
            bucket = self._alias_index.setdefault((alias.kind, normalized), set())
            bucket.add(alias.organization)

    @staticmethod
    def _alias_token(kind: TargetAliasKind, alias: str) -> str:
        if kind is TargetAliasKind.EXTERNAL_ID:
            # External ids are matched exactly (case-sensitive): ids encode
            # meaning in case.
            return str(alias)
        if kind is TargetAliasKind.DOMAIN:
            return normalize_domain(alias)
        return normalize_token(alias)

    # -- source resolution ----------------------------------------------------

    def _resolve_source(self):
        """Resolve the configured rating source org once, defensively."""
        name = sanitize_label(self.config.source_organization or "")
        if not name:
            self._source = (ResolutionReason.SOURCE_UNKNOWN,
                            "no rating source configured")
            return
        org = self._org_model.objects.filter(name=name).order_by("id").first()
        if org is None:
            self._source = (ResolutionReason.SOURCE_UNKNOWN,
                            f"no organization named '{name}' exists")
            return
        if org.status != ACTIVE_STATUS:
            self._source = (ResolutionReason.SOURCE_INACTIVE,
                            f"rating source '{name}' is inactive")
            return
        if not org.gives_ratings:
            self._source = (ResolutionReason.SOURCE_NOT_RATING,
                            f"'{name}' is not a rating source")
            return
        self._source = org

    # -- type resolution -------------------------------------------------------

    def _resolve_type(self, obs: RawScoreObservation):
        token = normalize_token(obs.external_type)
        mapping = None
        source_token = normalize_token(self.config.source_key)
        for m in self.config.score_type_mappings:
            if m.source_key == source_token and m.external_type == token:
                mapping = m
                break
        if mapping is None:
            return None, ResolutionReason.TYPE_UNKNOWN, (
                f"no score type mapping for external type "
                f"'{sanitize_label(obs.external_type)}'")
        st = self._active_types.get(mapping.score_type)
        if st is None:
            existing = self._type_model.objects.filter(
                name=mapping.score_type).order_by("id").first()
            if existing is not None:
                return None, ResolutionReason.TYPE_INACTIVE, (
                    f"score type '{sanitize_label(mapping.score_type)}' is inactive")
            return None, ResolutionReason.TYPE_UNKNOWN, (
                f"no active score type '{sanitize_label(mapping.score_type)}'")
        return st, None, ""

    # -- target resolution -------------------------------------------------------

    def _candidates_for(self, kind: TargetAliasKind, token: str) -> List[str]:
        names = set(self._alias_index.get((kind, token), ()))
        if kind is TargetAliasKind.DOMAIN:
            for org in self._active_orgs.values():
                if org.url and normalize_domain(org.url) == token:
                    names.add(org.name)
        elif kind is TargetAliasKind.NAME:
            for org in self._active_orgs.values():
                if normalize_token(org.name) == token:
                    names.add(org.name)
        return sorted(names)

    def _resolve_target(self, obs: RawScoreObservation):
        """Resolve target via external_id -> domain -> name, exact and ordered.

        Resolution order is fixed and documented: at the *first* alias kind that
        yields any candidate, exactly one active organization resolves; more
        than one resolves as ambiguous; a candidate whose organizations are
        inactive is reported inactive. Lower-priority kinds are consulted only
        when the higher-priority kind yields no candidate at all.
        """
        raw = str(obs.target or "")
        domain_token = normalize_domain(raw)
        name_token = normalize_token(raw)
        for kind, token in (
            (TargetAliasKind.EXTERNAL_ID, raw),
            (TargetAliasKind.DOMAIN, domain_token),
            (TargetAliasKind.NAME, name_token),
        ):
            candidates = self._candidates_for(kind, token)
            if not candidates:
                continue
            resolved = [self._active_orgs[n] for n in candidates
                        if n in self._active_orgs]
            inactive_hit = len(candidates) > len(resolved)
            if len(resolved) == 1:
                return resolved[0], None, ""
            if len(resolved) > 1:
                return None, ResolutionReason.TARGET_AMBIGUOUS, (
                    f"target '{sanitize_label(raw)}' matches multiple "
                    "active organizations")
            if inactive_hit:
                return None, ResolutionReason.TARGET_INACTIVE, (
                    f"organizations matched for '{sanitize_label(raw)}' "
                    "are inactive")
        return None, ResolutionReason.TARGET_UNKNOWN, (
            f"no active organization matches token '{sanitize_label(raw)}'")

    # -- numeric normalization ---------------------------------------------------

    def _parse_decimal(self, token: Optional[str], reason) -> Optional[Decimal]:
        """Parse one numeric token with reject-not-clamp rules, or raise."""
        if token is None:
            return None
        text = (token or "").strip()
        if not text:
            return None
        if len(text) > self.config.max_string_length:
            raise _InvalidNumber(reason, "token too long")
        try:
            value = Decimal(text)
        except InvalidOperation:
            raise _InvalidNumber(reason, "not a decimal") from None
        if not value.is_finite():
            raise _InvalidNumber(reason, "non-finite")
        if abs(value) > self.config.max_value_magnitude:
            raise _InvalidNumber(reason, "magnitude out of bounds")
        if self._significant_digits(value) > self.config.max_decimal_significant_digits:
            raise _InvalidNumber(reason, "too many significant digits")
        return value

    @staticmethod
    def _significant_digits(value: Decimal) -> int:
        digits = [int(d) for d in value.as_tuple().digits]
        while digits and digits[0] == 0:
            digits.pop(0)
        return max(len(digits), 1)

    def _normalize_numeric(self, obs: RawScoreObservation):
        """Return (value, low, high) or raise _InvalidNumber with a reason."""
        if all(t is None or not str(t).strip()
               for t in (obs.value, obs.low, obs.high)):
            raise _InvalidNumber(ResolutionReason.VALUE_MISSING,
                                 "observation carries neither a value nor a range")
        value = self._parse_decimal(obs.value, ResolutionReason.VALUE_INVALID)
        low = self._parse_decimal(obs.low, ResolutionReason.RANGE_INVALID)
        high = self._parse_decimal(obs.high, ResolutionReason.RANGE_INVALID)
        has_value = value is not None
        has_low = low is not None
        has_high = high is not None
        # A range must be a low/high pair; partial bounds are malformed.
        if has_low != has_high:
            raise _InvalidNumber(ResolutionReason.RANGE_INVALID,
                                 "partial range (both low and high are required)")
        if has_low and has_high and low > high:
            raise _InvalidNumber(ResolutionReason.RANGE_INVALID,
                                 "range low > high")
        if has_value and has_low and not (low <= value <= high):
            raise _InvalidNumber(ResolutionReason.VALUE_OUT_OF_RANGE,
                                 "value outside stated range (rejected, not clamped)")
        return value, low, high

    # -- single observation -------------------------------------------------------

    def normalize_one(self, obs: RawScoreObservation) -> ObservationOutcome:
        """Resolve + normalize one raw observation to a reason-coded outcome."""
        config = self.config
        key = observation_key(obs)

        source = self._source
        if isinstance(source, tuple):
            reason, detail = source
            return ObservationOutcome(
                key=key, status=reason.status, reason=reason,
                observation=None, detail=sanitize_label(detail),
            )

        if obs.source_key and normalize_token(obs.source_key) != \
                normalize_token(config.source_key):
            # A raw observation explicitly flagged for a different source
            # cannot be normalized under this config: reason-coded, never written.
            return ObservationOutcome(
                key=key, status=ResolutionStatus.REJECTED,
                reason=ResolutionReason.INPUT_LIMIT_EXCEEDED,
                observation=None, detail=("observation source mismatch"),
            )

        st, type_reason, type_detail = self._resolve_type(obs)
        if type_reason is not None:
            return ObservationOutcome(
                key=key, status=type_reason.status, reason=type_reason,
                observation=None, detail=sanitize_label(type_detail),
            )

        target_org, target_reason, target_detail = self._resolve_target(obs)
        if target_reason is not None:
            return ObservationOutcome(
                key=key, status=target_reason.status, reason=target_reason,
                observation=None, detail=sanitize_label(target_detail),
            )

        try:
            value, low, high = self._normalize_numeric(obs)
        except _InvalidNumber as exc:
            return ObservationOutcome(
                key=key, status=exc.reason.status, reason=exc.reason,
                observation=None, detail=sanitize_label(str(exc)),
            )

        observation = NormalizedScoreObservation(
            mapping_version=config.version,
            source_key=config.source_key,
            source_id=source.pk,
            source_name=source.name,
            type_id=st.pk,
            type_name=st.name,
            target_id=target_org.pk,
            target_name=target_org.name,
            value=value,
            low_threshold=low,
            high_threshold=high,
            raw_value=str(obs.value) if obs.value is not None else "",
            raw_low=str(obs.low) if obs.low is not None else "",
            raw_high=str(obs.high) if obs.high is not None else "",
            source_url=str(obs.source_url or ""),
            external_source_id=str(obs.external_source_id or ""),
            observed_at=obs.observed_at,
            fetched_at=obs.fetched_at,
            adapter=str(obs.adapter or "unknown"),
            adapter_version=str(obs.adapter_version or ""),
            run_correlation_id=str(obs.run_correlation_id or ""),
            external_type=str(obs.external_type or ""),
            external_target=str(obs.target or ""),
        )
        return ObservationOutcome(
            key=key, status=ResolutionStatus.NORMALIZED,
            reason=ResolutionReason.RESOLVED,
            observation=observation, detail="",
        )

    # -- batch -------------------------------------------------------------------

    def normalize(self, observations: Sequence[RawScoreObservation]) -> ResolutionReport:
        """Normalize a batch deterministically and return aggregate counts.

        Duplicate logical observations (same stable key) are counted once;
        identical repeats are skipped and recorded in ``duplicates_skipped`` so
        replaying a feed never double-counts. Outcomes preserve first-seen
        input order. Inputs beyond ``config.max_observations`` are reason-coded
        rejected rather than dropped silently.
        """
        config = self.config
        seen_keys = set()
        outcomes: List[ObservationOutcome] = []
        counts = {r.value: 0 for r in ResolutionReason}
        duplicates = 0
        beyond_limit = 0

        for obs in observations:
            key = observation_key(obs)
            if key in seen_keys:
                duplicates += 1
                continue
            seen_keys.add(key)
            if len(outcomes) >= config.max_observations:
                beyond_limit += 1
                continue
            outcome = self.normalize_one(obs)
            outcomes.append(outcome)
            counts[outcome.reason.value] += 1

        report = self._build_report(
            observations, outcomes, counts, duplicates, beyond_limit,
        )
        logger.info(
            "normalized batch: version=%s normalized=%d unresolved=%d "
            "rejected=%d duplicates=%d limit_exceeded=%d",
            config.version, report.normalized, report.unresolved,
            report.rejected, report.duplicates_skipped, report.skipped_limit,
        )
        for reason, n in report.counts.items():
            if n:
                logger.info("normalize reason %s count=%d", reason, n)
        return report

    def _build_report(self, observations, outcomes, counts, duplicates,
                      beyond_limit) -> ResolutionReport:
        normalized = counts[ResolutionReason.RESOLVED.value]
        unresolved = sum(
            counts[r.value] for r in ResolutionReason
            if r.status is ResolutionStatus.UNRESOLVED
        )
        rejected = sum(
            counts[r.value] for r in ResolutionReason
            if r.status is ResolutionStatus.REJECTED
        )
        if beyond_limit:
            counts[ResolutionReason.INPUT_LIMIT_EXCEEDED.value] += beyond_limit
            rejected += beyond_limit
        return ResolutionReport(
            mapping_version=self.config.version,
            inputs_seen=len(observations),
            duplicates_skipped=duplicates,
            normalized=normalized,
            unresolved=unresolved,
            rejected=rejected,
            counts=dict(counts),
            outcomes=tuple(outcomes),
            skipped_limit=beyond_limit,
        )


def normalize_observations(
    observations: Iterable[RawScoreObservation],
    config: ResolutionConfig,
    *,
    organization_model=None,
    score_type_model=None,
) -> ResolutionReport:
    """Construct a normalizer for ``config`` and normalize ``observations``.

    Returns a :class:`ResolutionReport` with typed outcomes and aggregate
    counts. Shortcut for the common single-pass case; use
    :class:`ScoreNormalizer` directly to reuse a snapshot across batches.
    """
    norm = ScoreNormalizer(
        config,
        organization_model=organization_model,
        score_type_model=score_type_model,
    )
    return norm.normalize(list(observations))
