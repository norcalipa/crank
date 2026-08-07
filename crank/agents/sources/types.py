# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Typed contracts for external score normalization and organization resolution.

These are the stable, database-agnostic boundaries for Phase 2 score gathering
(issues #311/#312/#313). They describe the raw (untrusted) external payloads,
the curated mapping configuration used to resolve identities, the normalized
typed observations that are safe to persist, and the reason-coded outcomes a
run can produce.

Design rules that keep this deterministic and safe:

* Dataclasses are frozen/hashable so identical values compare equal and a
  ``mapping version + input`` pair always yields the same output.
* Every model reference is stored as an integer primary key plus a display
  name so outcomes survive model renames and never carry ORM objects.
* Untrusted text (external type/target tokens, raw value strings) is held only
  for matching and provenance; helpers that render labels strip HTML/control
  characters and bound length so logs cannot leak HTML or secrets.
"""
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping, Optional, Sequence, Tuple


class ResolutionStatus(str, Enum):
    """High-level outcome of resolving + normalizing one observation."""

    NORMALIZED = "normalized"
    UNRESOLVED = "unresolved"
    REJECTED = "rejected"


class ResolutionReason(str, Enum):
    """Exact reason code attached to every outcome.

    ``UNRESOLVED`` reasons cover identity resolution failures (the observation
    is well-formed but no stable application identity could be chosen).
    ``REJECTED`` reasons cover malformed or out-of-policy numeric values that
    must never be written as scores. ``RESOLVED`` is the success code.
    """

    # Normalized ------------------------------------------------------------
    RESOLVED = "resolved"

    # Unresolved - rating source -------------------------------------------
    SOURCE_UNKNOWN = "source_unknown"
    SOURCE_INACTIVE = "source_inactive"
    SOURCE_NOT_RATING = "source_not_rating"

    # Unresolved - score type ----------------------------------------------
    TYPE_UNKNOWN = "type_unknown"
    TYPE_INACTIVE = "type_inactive"

    # Unresolved - target ---------------------------------------------------
    TARGET_UNKNOWN = "target_unknown"
    TARGET_AMBIGUOUS = "target_ambiguous"
    TARGET_INACTIVE = "target_inactive"

    # Rejected - value / range ---------------------------------------------
    VALUE_MISSING = "value_missing"
    VALUE_INVALID = "value_invalid"
    RANGE_INVALID = "range_invalid"
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    INPUT_LIMIT_EXCEEDED = "input_limit_exceeded"

    @property
    def status(self) -> "ResolutionStatus":
        """The coarse status this reason maps to."""
        if self is ResolutionReason.RESOLVED:
            return ResolutionStatus.NORMALIZED
        if self in {
            ResolutionReason.SOURCE_UNKNOWN,
            ResolutionReason.SOURCE_INACTIVE,
            ResolutionReason.SOURCE_NOT_RATING,
            ResolutionReason.TYPE_UNKNOWN,
            ResolutionReason.TYPE_INACTIVE,
            ResolutionReason.TARGET_UNKNOWN,
            ResolutionReason.TARGET_AMBIGUOUS,
            ResolutionReason.TARGET_INACTIVE,
        }:
            return ResolutionStatus.UNRESOLVED
        return ResolutionStatus.REJECTED


class TargetAliasKind(str, Enum):
    """The kind of external token a curated alias matches."""

    EXTERNAL_ID = "external_id"
    DOMAIN = "domain"
    NAME = "name"


@dataclass(frozen=True)
class TargetAlias:
    """A curated external token that resolves to one known organization.

    ``organization`` is matched against ``Organization.name`` verbatim. Only
    exactly one active organization may own a resolved token; a token that
    could resolve to more than one organization is reported ambiguous and never
    auto-picked.
    """

    kind: TargetAliasKind
    alias: str
    organization: str


@dataclass(frozen=True)
class ScoreTypeMapping:
    """Explicit mapping from an external score type to an active ``ScoreType``.

    The mapping is scoped to one rating source (``source_key``) so the same
    external token can mean different things per source without ambiguity.
    """

    source_key: str
    external_type: str
    score_type: str  # matched against ScoreType.name verbatim.


@dataclass(frozen=True)
class ResolutionConfig:
    """Curated, immutable configuration for one resolution pass.

    This is the single source of truth that makes replay deterministic: the
    same ``version`` plus the same input observation produces the same
    outcomes. Aliases and mappings are validated on construction so a bad
    approved config fails fast rather than producing subtly different results.
    """

    version: str = "1"
    source_key: str = "default"
    # Organization.name (verbatim) of the rating source organization.
    source_organization: Optional[str] = None
    score_type_mappings: Tuple[ScoreTypeMapping, ...] = ()
    target_aliases: Tuple[TargetAlias, ...] = ()
    # Numeric normalization bounds (reject, never clamp).
    max_value_magnitude: Decimal = Decimal("1e15")
    max_decimal_significant_digits: int = 30
    # Bounding for untrusted text and batch size.
    max_string_length: int = 512
    max_observations: int = 100000

    def __post_init__(self):
        if not self.version.strip():
            raise ValueError("ResolutionConfig.version must not be blank")
        if self.max_decimal_significant_digits <= 0:
            raise ValueError("max_decimal_significant_digits must be positive")
        if self.max_string_length <= 0:
            raise ValueError("max_string_length must be positive")
        if self.max_observations <= 0:
            raise ValueError("max_observations must be positive")
        try:
            mag = Decimal(self.max_value_magnitude)
            if not mag.is_finite() or mag <= 0:
                raise ValueError("not finite/positive")
        except Exception as exc:  # noqa: BLE001 - normalizing into a clear error
            raise ValueError(
                f"max_value_magnitude must be a positive finite Decimal: {exc}"
            ) from exc


@dataclass(frozen=True)
class RawScoreObservation:
    """One raw (untrusted) external score observation, pre-normalization.

    ``value``/``low``/``high`` are kept as the source's own strings; parsing to
    ``Decimal`` happens later with reject-not-clamp rules. All text fields are
    external input and must be treated as untrusted (bounded, never rendered as
    HTML, never logged verbatim).
    """

    source_key: str = ""
    external_type: str = ""
    # The external token that should resolve to a target organization. It is
    # matched as an external id, then a domain, then a name, in that order.
    target: str = ""
    value: Optional[str] = None
    low: Optional[str] = None
    high: Optional[str] = None
    source_url: str = ""
    external_source_id: str = ""
    observed_at: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    adapter: str = "unknown"
    adapter_version: str = ""
    run_correlation_id: str = ""


@dataclass(frozen=True)
class NormalizedScoreObservation:
    """A fully resolved, normalized observation ready for persistence/provenance.

    Model references are stored as integer pks (plus names for display) so the
    object is plain data. Raw strings are preserved alongside normalized
    ``Decimal`` values to satisfy the provenance requirement without storing a
    whole source payload. ``value`` is ``None`` when only a range (``low``/
    ``high``) is available.
    """

    mapping_version: str
    source_key: str
    source_id: Optional[int]
    source_name: str
    type_id: Optional[int]
    type_name: str
    target_id: Optional[int]
    target_name: str
    value: Optional[Decimal] = None
    low_threshold: Optional[Decimal] = None
    high_threshold: Optional[Decimal] = None
    raw_value: str = ""
    raw_low: str = ""
    raw_high: str = ""
    source_url: str = ""
    external_source_id: str = ""
    observed_at: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    adapter: str = "unknown"
    adapter_version: str = ""
    run_correlation_id: str = ""
    external_type: str = ""
    external_target: str = ""


@dataclass(frozen=True)
class ObservationOutcome:
    """Reason-coded result of resolving/normalizing one observation."""

    key: str
    status: ResolutionStatus
    reason: ResolutionReason
    observation: Optional[NormalizedScoreObservation] = None
    # Short, sanitized explanation. Never contains raw untrusted HTML/secrets.
    detail: str = ""


@dataclass(frozen=True)
class ResolutionReport:
    """Aggregate counts plus every per-observation outcome for a run."""

    mapping_version: str
    inputs_seen: int = 0
    duplicates_skipped: int = 0
    normalized: int = 0
    unresolved: int = 0
    rejected: int = 0
    counts: Mapping[str, int] = field(default_factory=dict)
    outcomes: Tuple[ObservationOutcome, ...] = ()
    skipped_limit: int = 0

    def by_reason(self, status: Optional[ResolutionStatus] = None) -> Mapping[str, int]:
        """Reason-count pairs, optionally narrowed to one status."""
        if status is None:
            return dict(self.counts)
        return {
            code: n for code, n in self.counts.items()
            if ResolutionReason(code).status is status
        }


def normalize_token(value: str, max_len: int = 512) -> str:
    """Unicode/case normalization for name and type matching.

    NFKC-normalizes, casefolds, and collapses internal whitespace so
    equivalent tokens match across Unicode forms and casing while staying
    bounded. Used for *matching only*; original text is preserved for
    provenance.
    """
    text = unicodedata.normalize("NFKC", value or "")
    text = text.casefold()
    text = " ".join(text.split())
    return text[:max_len]


def normalize_domain(value: str, max_len: int = 512) -> str:
    """Normalize a URL/domain token to a comparable hostname.

    Strips scheme, path, query, fragment and a trailing dot, lowercases, and
    punycodes the host. Non-convertible input degrades to the stripped/lower
    string so matching never raises. Exact-match only; no fuzzy matching.
    """
    host = (value or "").strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.strip().lower().rstrip(".")
    # Drop a trailing ``:port`` so a URL host matches a bare domain alias.
    if ":" in host:
        head, _, tail = host.rpartition(":")
        if tail.isdigit():
            host = head
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return host[:max_len]


def sanitize_label(value: str, max_len: int = 512) -> str:
    """Make an untrusted label safe to embed in logs/detail text.

    Strips any HTML-ish tags (``<...>``), removes control characters, and
    collapses whitespace so the result cannot render as markup or smuggle
    control sequences, and is length-bounded.
    """
    text = (value or "").replace("<", "").replace(">", "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc")
    text = " ".join(text.split())
    return text[:max_len]


def observation_key(obs: RawScoreObservation) -> str:
    """A stable digest identifying this logical observation for dedup/replay.

    Excludes volatile provenance (``fetched_at``, ``run_correlation_id``,
    ``adapter``) so the same logical observation replayed in a later fetch
    deduplicates to the same key. Includes ``observed_at`` because when an
    observation happened is semantically meaningful.
    """
    import hashlib
    import json

    payload = {
        "source_key": obs.source_key,
        "external_type": obs.external_type,
        "target": obs.target,
        "value": obs.value,
        "low": obs.low,
        "high": obs.high,
        "source_url": obs.source_url,
        "external_source_id": obs.external_source_id,
        "observed_at": obs.observed_at.isoformat() if obs.observed_at else None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Sentinel distinguishing "caller did not pass this bound" from an explicit
# None. Allows build_resolution_config to fill sensible defaults while still
# passing through caller-provided overrides.
_UNSET = object()


def build_resolution_config(
    *,
    version: str = "1",
    source_key: str = "default",
    source_organization: Optional[str] = None,
    score_type_mappings: Sequence[Mapping] = (),
    target_aliases: Sequence[Mapping] = (),
    max_value_magnitude=_UNSET,
    max_decimal_significant_digits=_UNSET,
    max_string_length=_UNSET,
    max_observations=_UNSET,
) -> ResolutionConfig:
    """Build and validate a :class:`ResolutionConfig` from plain mappings.

    Accepts ``sequence`` of dicts for type mappings and target aliases so
    settings-loaded configuration (JSON/YAML-style data) can be validated into
    the frozen typed form in one place. Fails fast on bad entries rather than
    silently dropping them.
    """
    types = []
    for m in score_type_mappings:
        source = normalize_token(str(m.get("source", "")))
        external = normalize_token(str(m.get("external", "")))
        score_type = str(m.get("score_type", "")).strip()
        if not source or not external or not score_type:
            raise ValueError(
                "score type mapping requires source, external, and score_type"
            )
        types.append(ScoreTypeMapping(source_key=source, external_type=external,
                                      score_type=score_type))
    aliases = []
    for a in target_aliases:
        try:
            kind = TargetAliasKind(str(a.get("kind", "")).strip())
        except ValueError:
            raise ValueError(
                f"target alias kind must be one of "
                f"{', '.join(k.value for k in TargetAliasKind)}"
            ) from None
        alias = str(a.get("alias", ""))
        organization = str(a.get("organization", "")).strip()
        if organization == "":
            raise ValueError("target alias requires a non-blank organization")
        aliases.append(TargetAlias(kind=kind, alias=alias, organization=organization))

    # Drop the sentinel default so a caller can build with sane bounds; the
    # sentinel only exists to distinguish "unset" from an explicit None.
    kwargs = {}
    if max_value_magnitude is not _UNSET:
        kwargs["max_value_magnitude"] = max_value_magnitude
    if max_decimal_significant_digits is not _UNSET:
        kwargs["max_decimal_significant_digits"] = max_decimal_significant_digits
    if max_string_length is not _UNSET:
        kwargs["max_string_length"] = max_string_length
    if max_observations is not _UNSET:
        kwargs["max_observations"] = max_observations
    return ResolutionConfig(
        version=str(version),
        source_key=normalize_token(str(source_key)),
        source_organization=(source_organization or "").strip() or None,
        score_type_mappings=types,
        target_aliases=aliases,
        **kwargs,
    )
