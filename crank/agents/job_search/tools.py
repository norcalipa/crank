# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Bounded, server-controlled data tools for the job-search orchestrator.

These are the **only** ways the model may learn about organizations and score
summaries. Inputs are validated against fixed schemas with fixed result limits;
organization/source text is treated as untrusted and relayed as-is. The default
``*_datasource`` implementations query only active/public ``Organization`` rows
(and their target ``Score`` rows) through the Django ORM; callers may inject
fakes for tests. There is intentionally no mechanism here for reaching
arbitrary models, SQL, files, hosts, or URLs.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

MAX_ORGANIZATION_RESULTS = 25
MAX_SCORE_SUMMARY_RESULTS = 5
MAX_FILTER_QUERY_LENGTH = 80

#: Filters the model may apply to the organization query. Anything else is rejected.
ALLOWED_ORGANIZATION_FILTERS = frozenset({"query", "funding_round", "rto_policy"})

#: Permissible values for filter enums (server-authoritative subsets of the
#: Organization.TextChoices values that the model may use).
FUNDING_ROUND_VALUES = frozenset(
    {"S", "A", "B", "C", "D", "E", "F", "X", "O", "P"}
)
RTO_POLICY_VALUES = frozenset({"R", "H", "O"})


class InvalidToolInputError(ValueError):
    """A tool invocation violated the bounded input schema."""


def validate_organization_filters(filters: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """Validate and normalize organization filter kwargs.

    Raises :class:`InvalidToolInputError` for unknown keys, non-string values,
    overlong queries, or enum values outside the server-defined set.
    """
    if not filters:
        return {}
    if not isinstance(filters, Mapping):
        raise InvalidToolInputError("organization filters must be a mapping")
    unknown = set(filters) - ALLOWED_ORGANIZATION_FILTERS
    if unknown:
        raise InvalidToolInputError(
            "unknown organization filter(s): %s" % ", ".join(sorted(unknown))
        )
    normalized: Dict[str, str] = {}
    for key in ("query", "funding_round", "rto_policy"):
        value = filters.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise InvalidToolInputError("filter %r must be a string" % key)
        value = value.strip()
        if not value:
            continue
        if key == "query":
            if len(value) > MAX_FILTER_QUERY_LENGTH:
                raise InvalidToolInputError(
                    "query filter exceeds %d characters" % MAX_FILTER_QUERY_LENGTH
                )
        elif key == "funding_round" and value not in FUNDING_ROUND_VALUES:
            raise InvalidToolInputError("invalid funding_round value %r" % value)
        elif key == "rto_policy" and value not in RTO_POLICY_VALUES:
            raise InvalidToolInputError("invalid rto_policy value %r" % value)
        normalized[key] = value
    return normalized


def clamp_result_limit(limit: Optional[int], *, maximum: int) -> int:
    """Clamp a requested result limit to the fixed maximum (and at least 1)."""
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(1, min(limit, maximum))
    return maximum


def normalize_organization_rows(rows: List[Any]) -> List[Dict[str, Any]]:
    """Project ORM rows to the safe, minimal dict the model sees."""
    output: List[Dict[str, Any]] = []
    for row in rows:
        output.append({
            "id": int(row.id),
            "name": str(row.name),
            "url": str(getattr(row, "url", "")),
            "funding_round": str(getattr(row, "funding_round", "")),
            "rto_policy": str(getattr(row, "rto_policy", "")),
        })
    return output


def normalize_score_summary_rows(rows: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """Project/validate score-summary rows to the canonical dict renderers expect.

    Score rows are injectable (``score_datasource`` may be faked in tests), so
    this validates shape up front: each row must be a dict with an integer
    ``organization_id``, a non-empty string ``score_type``, and a numeric
    ``avg_score``. A malformed row raises :class:`InvalidScoreSummaryRowError`
    rather than surfacing later as a bare ``KeyError`` during rendering.
    """
    from crank.agents.job_search.errors import InvalidScoreSummaryRowError

    output: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            raise InvalidScoreSummaryRowError(
                "score summary rows must be dicts, got %s" % type(row).__name__
            )
        org_id = row.get("organization_id")
        score_type = row.get("score_type")
        avg_score = row.get("avg_score")
        if (
            isinstance(org_id, bool)
            or not isinstance(org_id, int)
            or not isinstance(score_type, str)
            or not score_type.strip()
            or isinstance(avg_score, bool)
            or not isinstance(avg_score, (int, float))
        ):
            raise InvalidScoreSummaryRowError(
                "score summary row must have integer organization_id, "
                "non-empty string score_type, and numeric avg_score; "
                "got organization_id=%r score_type=%r avg_score=%r"
                % (org_id, score_type, avg_score)
            )
        output.append({
            "organization_id": int(org_id),
            "score_type": score_type.strip(),
            "avg_score": float(avg_score),
        })
    return output


def default_organization_datasource(
    filters: Mapping[str, Any], limit: int
) -> List[Any]:
    """Server-controlled query: active, public organizations only.

    Import is deferred so importing this package never requires Django settings
    to be configured. Filters are already validated by the caller.
    """
    from crank.models.organization import Organization

    queryset = Organization.objects.filter(status=1, public=True)
    if filters.get("query"):
        queryset = queryset.filter(name__icontains=filters["query"])
    if filters.get("funding_round"):
        queryset = queryset.filter(funding_round=filters["funding_round"])
    if filters.get("rto_policy"):
        queryset = queryset.filter(rto_policy=filters["rto_policy"])
    return list(queryset[:limit])


def query_active_organizations(
    filters: Optional[Mapping[str, Any]] = None,
    limit: Optional[int] = None,
    datasource: Optional[callable] = None,
) -> List[Dict[str, Any]]:
    """Validate the request and return bounded, normalized organization rows.

    This is the public "tool" surface. ``datasource`` defaults to the Django
    ORM query (active/public only) and may be injected in tests.
    """
    normalized = validate_organization_filters(filters)
    capped = clamp_result_limit(limit, maximum=MAX_ORGANIZATION_RESULTS)
    loader = datasource or default_organization_datasource
    rows = loader(normalized, capped)
    return normalize_organization_rows(rows)


def validate_score_summary_input(
    organization_ids: Any, score_types: Optional[Any]
) -> List[int]:
    """Validate the score-summary tool input.

    ``organization_ids`` must be a non-empty flat list of unique integers.
    Raises :class:`InvalidToolInputError` otherwise.
    """
    if not isinstance(organization_ids, (list, tuple)) or not organization_ids:
        raise InvalidToolInputError("score_summary requires a non-empty organization_ids list")
    ids: List[int] = []
    for value in organization_ids:
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidToolInputError("organization_ids must be integers")
        ids.append(value)
    if len(set(ids)) != len(ids):
        raise InvalidToolInputError("organization_ids must be unique")
    return ids


def default_score_summary_datasource(
    organization_ids: List[int],
    score_types: Optional[List[str]],
    limit: int,
) -> List[Any]:
    """Server-controlled query: average score summaries for the given targets."""
    from django.db.models import Avg

    from crank.models.score import Score, ScoreType

    base = Score.objects.filter(status=1, target_id__in=organization_ids)
    if score_types:
        type_pks = list(
            ScoreType.objects.filter(status=1, name__in=score_types).values_list("pk", flat=True)
        )
        base = base.filter(type_id__in=type_pks)
    grouped = base.values("target_id", "type__name").annotate(avg_score=Avg("score"))
    results = list(grouped[:limit])
    return [
        {
            "organization_id": int(row["target_id"]),
            "score_type": str(row["type__name"]),
            "avg_score": float(row["avg_score"] or 0.0),
        }
        for row in results
    ]


def query_score_summaries(
    organization_ids: Any,
    score_types: Optional[Any] = None,
    limit: Optional[int] = None,
    datasource: Optional[callable] = None,
) -> List[Dict[str, Any]]:
    """Validate the request and return bounded score summaries.

    The set of allowed ``organization_ids`` is whatever the caller already
    validated against the active-organization tool; this tool does not expand
    visibility beyond those IDs.
    """
    ids = validate_score_summary_input(organization_ids, score_types)
    capped = clamp_result_limit(limit, maximum=MAX_SCORE_SUMMARY_RESULTS)
    loader = datasource or default_score_summary_datasource
    return loader(ids, score_types, capped)


def union_server_controlled_ids(rows: List[Dict[str, Any]]) -> List[int]:
    """Return the sorted IDs the server actually exposed via the tools."""
    return sorted({int(row.get("id")) for row in rows if row.get("id") is not None})