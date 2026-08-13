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

from collections.abc import Mapping
from typing import Any

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


def validate_organization_filters(filters: Mapping[str, Any] | None) -> dict[str, str]:
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
            f"unknown organization filter(s): {', '.join(sorted(unknown))}"
        )
    normalized: dict[str, str] = {}
    for key in ("query", "funding_round", "rto_policy"):
        value = filters.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise InvalidToolInputError(f"filter {key!r} must be a string")
        value = value.strip()
        if not value:
            continue
        if key == "query":
            if len(value) > MAX_FILTER_QUERY_LENGTH:
                raise InvalidToolInputError(
                    f"query filter exceeds {MAX_FILTER_QUERY_LENGTH} characters"
                )
        elif key == "funding_round" and value not in FUNDING_ROUND_VALUES:
            raise InvalidToolInputError(f"invalid funding_round value {value!r}")
        elif key == "rto_policy" and value not in RTO_POLICY_VALUES:
            raise InvalidToolInputError(f"invalid rto_policy value {value!r}")
        normalized[key] = value
    return normalized


def clamp_result_limit(limit: int | None, *, maximum: int) -> int:
    """Clamp a requested result limit to the fixed maximum (and at least 1)."""
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(1, min(limit, maximum))
    return maximum


def normalize_organization_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Project ORM rows to the safe, minimal dict the model sees."""
    output: list[dict[str, Any]] = []
    for row in rows:
        output.append({
            "id": int(row.id),
            "name": str(row.name),
            "url": str(getattr(row, "url", "")),
            "funding_round": str(getattr(row, "funding_round", "")),
            "rto_policy": str(getattr(row, "rto_policy", "")),
        })
    return output


def normalize_score_summary_rows(rows: list[Any] | None) -> list[dict[str, Any]]:
    """Project/validate score-summary rows to the canonical dict renderers expect.

    Score rows are injectable (``score_datasource`` may be faked in tests), so
    this validates shape up front: each row must be a dict with an integer
    ``organization_id``, a non-empty string ``score_type``, and a numeric
    ``avg_score``. A malformed row raises :class:`InvalidScoreSummaryRowError`
    rather than surfacing later as a bare ``KeyError`` during rendering.
    """
    from crank.agents.job_search.errors import InvalidScoreSummaryRowError

    output: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            raise InvalidScoreSummaryRowError(
                f"score summary rows must be dicts, got {type(row).__name__}"
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
                f"got organization_id={org_id!r} score_type={score_type!r} avg_score={avg_score!r}"
            )
        output.append({
            "organization_id": int(org_id),
            "score_type": score_type.strip(),
            "avg_score": float(avg_score),
        })
    return output


def default_organization_datasource(
    filters: Mapping[str, Any], limit: int
) -> list[Any]:
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
    filters: Mapping[str, Any] | None = None,
    limit: int | None = None,
    datasource: callable | None = None,
) -> list[dict[str, Any]]:
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
    organization_ids: Any, score_types: Any | None
) -> list[int]:
    """Validate the score-summary tool input.

    ``organization_ids`` must be a non-empty flat list of unique integers.
    Raises :class:`InvalidToolInputError` otherwise.
    """
    if not isinstance(organization_ids, (list, tuple)) or not organization_ids:
        raise InvalidToolInputError("score_summary requires a non-empty organization_ids list")
    ids: list[int] = []
    for value in organization_ids:
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidToolInputError("organization_ids must be integers")
        ids.append(value)
    if len(set(ids)) != len(ids):
        raise InvalidToolInputError("organization_ids must be unique")
    return ids


def default_score_summary_datasource(
    organization_ids: list[int],
    score_types: list[str] | None,
    limit: int,
) -> list[Any]:
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
    score_types: Any | None = None,
    limit: int | None = None,
    datasource: callable | None = None,
) -> list[dict[str, Any]]:
    """Validate the request and return bounded score summaries.

    The set of allowed ``organization_ids`` is whatever the caller already
    validated against the active-organization tool; this tool does not expand
    visibility beyond those IDs.
    """
    ids = validate_score_summary_input(organization_ids, score_types)
    capped = clamp_result_limit(limit, maximum=MAX_SCORE_SUMMARY_RESULTS)
    loader = datasource or default_score_summary_datasource
    return loader(ids, score_types, capped)


def union_server_controlled_ids(rows: list[dict[str, Any]]) -> list[int]:
    """Return the sorted IDs the server actually exposed via the tools."""
    return sorted({int(row.get("id")) for row in rows if row.get("id") is not None})


# ---------------------------------------------------------------------------
# Job-listing tools (issue #393)
# ---------------------------------------------------------------------------

MAX_JOB_LISTING_RESULTS = 25
MAX_JOB_LISTING_QUERY_LENGTH = 120
MAX_DESCRIPTION_EXCERPT_LENGTH = 500

#: Filters the model may apply to the job-listing search. Anything else is
#: rejected so the model cannot inject arbitrary ORM lookups.
ALLOWED_JOB_LISTING_FILTERS = frozenset(
    {"query", "location", "remote", "min_compensation", "organization_id", "status"}
)

#: The only ``status`` value the model may request. The server enforces
#: ``open``/active regardless, but accepting the key keeps the tool interface
#: explicit about what the model can ask for.
JOB_LISTING_STATUS_VALUES = frozenset({"open"})


def validate_job_listing_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize job-listing filter kwargs.

    Raises :class:`InvalidToolInputError` for unknown keys, overlong
    queries, non-boolean ``remote``, non-integer ``min_compensation`` or
    ``organization_id``, or invalid ``status`` values.
    """
    if not filters:
        return {}
    if not isinstance(filters, Mapping):
        raise InvalidToolInputError("job listing filters must be a mapping")
    unknown = set(filters) - ALLOWED_JOB_LISTING_FILTERS
    if unknown:
        raise InvalidToolInputError(
            f"unknown job listing filter(s): {', '.join(sorted(unknown))}"
        )
    normalized: dict[str, Any] = {}
    for key in ("query", "location"):
        value = filters.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise InvalidToolInputError(f"filter {key!r} must be a string")
        value = value.strip()
        if not value:
            continue
        if len(value) > MAX_JOB_LISTING_QUERY_LENGTH:
            raise InvalidToolInputError(
                f"{key} filter exceeds {MAX_JOB_LISTING_QUERY_LENGTH} characters"
            )
        normalized[key] = value
    # remote: must be a real bool
    remote = filters.get("remote")
    if remote is not None:
        if not isinstance(remote, bool):
            raise InvalidToolInputError("filter 'remote' must be a boolean")
        normalized["remote"] = remote
    # min_compensation: positive integer
    min_comp = filters.get("min_compensation")
    if min_comp is not None:
        if isinstance(min_comp, bool) or not isinstance(min_comp, int):
            raise InvalidToolInputError("filter 'min_compensation' must be an integer")
        if min_comp < 0:
            raise InvalidToolInputError("filter 'min_compensation' must be non-negative")
        normalized["min_compensation"] = min_comp
    # organization_id: positive integer
    org_id = filters.get("organization_id")
    if org_id is not None:
        if isinstance(org_id, bool) or not isinstance(org_id, int):
            raise InvalidToolInputError("filter 'organization_id' must be an integer")
        if org_id < 1:
            raise InvalidToolInputError("filter 'organization_id' must be positive")
        normalized["organization_id"] = org_id
    # status: only "open" is allowed
    status = filters.get("status")
    if status is not None:
        if not isinstance(status, str):
            raise InvalidToolInputError("filter 'status' must be a string")
        status = status.strip()
        if status not in JOB_LISTING_STATUS_VALUES:
            raise InvalidToolInputError(
                f"invalid status value {status!r}; allowed: {', '.join(sorted(JOB_LISTING_STATUS_VALUES))}"
            )
        normalized["status"] = status
    return normalized


def normalize_job_listing_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Project ORM ``JobListing`` rows to the safe, minimal dict the model sees.

    Untrusted text (title, employer name, location) is relayed as-is; the
    canonical URL always comes from the row, never invented by the model.
    """
    output: list[dict[str, Any]] = []
    for row in rows:
        comp_min = getattr(row, "compensation_min", None)
        comp_max = getattr(row, "compensation_max", None)
        comp_currency = getattr(row, "compensation_currency", "") or ""
        comp_interval = getattr(row, "compensation_interval", "") or ""
        compensation: dict[str, Any] | None
        if comp_min is not None or comp_max is not None:
            compensation = {
                "min": float(comp_min) if comp_min is not None else None,
                "max": float(comp_max) if comp_max is not None else None,
                "currency": str(comp_currency),
                "interval": str(comp_interval),
            }
        else:
            compensation = None
        org = getattr(row, "organization", None)
        output.append({
            "id": int(row.id),
            "title": str(getattr(row, "title", "")),
            "organization_name": str(getattr(org, "name", "")) if org is not None else "",
            "organization_id": int(org.id) if org is not None and getattr(org, "id", None) is not None else None,
            "location": str(getattr(row, "location_text", "")),
            "remote": bool(getattr(row, "is_remote", False)),
            "compensation": compensation,
            "canonical_url": str(getattr(row, "canonical_url", "")),
            "observed_at": _iso_or_none(getattr(row, "last_seen_at", None)),
            "updated_at": _iso_or_none(getattr(row, "modified", None)),
        })
    return output


def _iso_or_none(value: Any) -> str | None:
    """Return an ISO-8601 string for a datetime, or ``None``."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def default_job_listing_datasource(
    filters: Mapping[str, Any], limit: int
) -> list[Any]:
    """Server-controlled query: active/open ``JobListing`` rows only.

    Import is deferred so importing this package never requires Django settings
    to be configured. Filters are already validated by the caller.
    """
    from crank.models.job import JobListing

    queryset = JobListing.objects.all()
    # The default manager already filters to active listings, but be explicit.
    queryset = queryset.filter(status=JobListing.Status.ACTIVE)
    if filters.get("query"):
        queryset = queryset.filter(title__icontains=filters["query"])
    if filters.get("location"):
        queryset = queryset.filter(location_text__icontains=filters["location"])
    if filters.get("remote"):
        queryset = queryset.filter(is_remote=True)
    if filters.get("min_compensation") is not None:
        queryset = queryset.filter(
            compensation_min__gte=filters["min_compensation"]
        )
    if filters.get("organization_id") is not None:
        queryset = queryset.filter(organization_id=filters["organization_id"])
    # Ordering by freshness (last_seen_at desc) is the model default.
    queryset = queryset.order_by("-last_seen_at", "-id")
    return list(queryset[:limit])


def search_job_listings(
    filters: Mapping[str, Any] | None = None,
    limit: int | None = None,
    datasource: callable | None = None,
) -> list[dict[str, Any]]:
    """Validate the request and return bounded, normalized job-listing rows.

    This is the public "tool" surface. ``datasource`` defaults to the Django
    ORM query (active listings only) and may be injected in tests.
    """
    normalized = validate_job_listing_filters(filters)
    capped = clamp_result_limit(limit, maximum=MAX_JOB_LISTING_RESULTS)
    loader = datasource or default_job_listing_datasource
    rows = loader(normalized, capped)
    return normalize_job_listing_rows(rows)


def default_job_listing_detail_datasource(
    listing_id: int,
) -> Any | None:
    """Server-controlled query: a single active ``JobListing`` by ID.

    Import is deferred so importing this package never requires Django
    settings to be configured.
    """
    from crank.models.job import JobListing

    return JobListing.objects.filter(
        id=listing_id, status=JobListing.Status.ACTIVE
    ).first()


def get_job_listing_detail(
    listing_id: int,
    datasource: callable | None = None,
) -> dict[str, Any] | None:
    """Validate the request and return a single job-listing row, or ``None``.

    ``datasource`` defaults to the Django ORM query (active listing by ID)
    and may be injected in tests.
    """
    if isinstance(listing_id, bool) or not isinstance(listing_id, int):
        raise InvalidToolInputError("listing_id must be an integer")
    if listing_id < 1:
        raise InvalidToolInputError("listing_id must be positive")
    loader = datasource or default_job_listing_detail_datasource
    row = loader(listing_id)
    if row is None:
        return None
    result = normalize_job_listing_rows([row])[0]
    # Add description excerpt for the detail view.
    excerpt = str(getattr(row, "description_excerpt", "") or "")
    if len(excerpt) > MAX_DESCRIPTION_EXCERPT_LENGTH:
        excerpt = excerpt[:MAX_DESCRIPTION_EXCERPT_LENGTH]
    result["description_excerpt"] = excerpt
    return result


def union_server_controlled_listing_ids(rows: list[dict[str, Any]]) -> list[int]:
    """Return the sorted listing IDs the server actually exposed via the tools."""
    return sorted({int(row.get("id")) for row in rows if row.get("id") is not None})


# ---------------------------------------------------------------------------
# Preference-grounded matching tools (issue #395)
# ---------------------------------------------------------------------------

MAX_MATCH_RESULTS = 25


def normalize_match_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project match result dicts to the safe, minimal shape the model sees."""
    output: list[dict[str, Any]] = []
    for row in rows:
        output.append({
            "listing_id": int(row.get("listing_id", 0)),
            "title": str(row.get("title", "")),
            "employer_name": str(row.get("employer_name", "")),
            "organization_id": row.get("organization_id"),
            "organization_name": str(row.get("organization_name", "")),
            "canonical_url": str(row.get("canonical_url", "")),
            "location": str(row.get("location_text", "")),
            "remote": bool(row.get("is_remote", False)),
            "score": float(row.get("score", 0.0)),
            "reasons": list(row.get("reasons", [])),
        })
    return output


def normalize_org_match_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project org match result dicts to the safe, minimal shape the model sees."""
    output: list[dict[str, Any]] = []
    for row in rows:
        output.append({
            "organization_id": int(row.get("organization_id", 0)),
            "name": str(row.get("name", "")),
            "url": str(row.get("url", "")),
            "funding_round": str(row.get("funding_round", "")),
            "rto_policy": str(row.get("rto_policy", "")),
            "score": float(row.get("score", 0.0)),
            "reasons": list(row.get("reasons", [])),
        })
    return output


def get_matches_for_user(
    user: Any,
    *,
    limit: int | None = None,
    match_service: callable | None = None,
) -> dict[str, Any]:
    """Return bounded, ranked job and organization matches for *user*.

    This is the public tool surface for preference-grounded matching. It
    delegates to :mod:`crank.services.job_matching` and returns normalized,
    bounded results with human-readable reasons. ``match_service`` may be
    injected in tests.
    """
    capped = clamp_result_limit(limit, maximum=MAX_MATCH_RESULTS)
    if match_service is not None:
        job_results, org_results = match_service(user, limit=capped)
    else:
        from crank.services.job_matching import match_jobs, match_organizations
        job_results = match_jobs(user, limit=capped)
        org_results = match_organizations(user, limit=capped)

    job_dicts = [
        {
            "listing_id": r.listing_id,
            "title": r.title,
            "employer_name": r.employer_name,
            "organization_id": r.organization_id,
            "organization_name": r.organization_name,
            "canonical_url": r.canonical_url,
            "location_text": r.location_text,
            "is_remote": r.is_remote,
            "score": r.score,
            "reasons": r.reasons,
        }
        for r in job_results
    ]
    org_dicts = [
        {
            "organization_id": r.organization_id,
            "name": r.name,
            "url": r.url,
            "funding_round": r.funding_round,
            "rto_policy": r.rto_policy,
            "score": r.score,
            "reasons": r.reasons,
        }
        for r in org_results
    ]
    return {
        "job_matches": normalize_match_rows(job_dicts),
        "organization_matches": normalize_org_match_rows(org_dicts),
    }
