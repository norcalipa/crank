# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Deterministic, offline benchmark scenarios for the job pipeline.

The benchmark intentionally does not call the database or a network adapter. It
runs the same normalization, organization-resolution, and ranking primitives
used by the scheduled pipeline against generated records. This makes the
capacity check safe to run in CI and repeatable in staging without credentials
or live-source traffic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import resource
import time
import tracemalloc
from types import SimpleNamespace
from typing import Any

from crank.agents.jobs.matching import project_criteria, rank_listings
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG


@dataclass(frozen=True)
class BenchmarkBudget:
    """Upper bounds for one generated workload."""

    run_window_ms: float
    peak_memory_mb: float
    cpu_seconds: float
    query_count: int
    external_calls: int


@dataclass(frozen=True)
class BenchmarkProfile:
    """Size and fault model for a repeatable workload."""

    name: str
    sources: int
    pages_per_source: int
    listings_per_page: int
    users: int
    transient_failures_per_source: int
    budget: BenchmarkBudget


PROFILES = {
    "ci": BenchmarkProfile(
        name="ci",
        sources=2,
        pages_per_source=2,
        listings_per_page=50,
        users=4,
        transient_failures_per_source=1,
        budget=BenchmarkBudget(
            run_window_ms=2000.0,
            peak_memory_mb=128.0,
            cpu_seconds=2.0,
            query_count=0,
            external_calls=0,
        ),
    ),
    "staging": BenchmarkProfile(
        name="staging",
        sources=3,
        pages_per_source=3,
        listings_per_page=100,
        users=10,
        transient_failures_per_source=1,
        budget=BenchmarkBudget(
            run_window_ms=10000.0,
            peak_memory_mb=512.0,
            cpu_seconds=10.0,
            query_count=0,
            external_calls=0,
        ),
    ),
}

_DEFAULT_PREFERENCES = {
    "compensation": {"minimum_salary": 100000, "currency": "USD"},
    "work_location": {"modes": ["remote"]},
    "geography": {"remote_friendly": True},
    "industry": ["software"],
    "funding_stage": ["series a"],
    "culture": ["remote-first"],
}


def get_profile(name: str) -> BenchmarkProfile:
    """Return a named profile or raise a useful argument error."""
    try:
        return PROFILES[name.lower()]
    except KeyError as exc:
        choices = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown benchmark profile {name!r}; choose {choices}") from exc


def _listing(listing_id: int, organization: Any) -> SimpleNamespace:
    return SimpleNamespace(
        pk=listing_id,
        id=listing_id,
        employer_name=f"Synthetic Org {listing_id % 32}",
        title="Senior Software Engineer",
        location_text="United States",
        is_remote=True,
        compensation_min=120000,
        compensation_max=160000,
        compensation_currency="USD",
        compensation_interval="year",
        description_excerpt="Remote-first software team",
        status="active",
        source_metadata={},
        organization=organization,
    )


def _synthetic_pages(profile: BenchmarkProfile, seed: int) -> list[list[dict[str, int]]]:
    """Create deterministic page records without random or external input."""
    pages: list[list[dict[str, int]]] = []
    listing_id = seed * 100000
    for source_id in range(profile.sources):
        for page_id in range(profile.pages_per_source):
            page = []
            for item_id in range(profile.listings_per_page):
                page.append(
                    {
                        "source_id": source_id,
                        "listing_id": listing_id,
                        "organization_id": (source_id * 32 + item_id) % 32,
                    }
                )
                listing_id += 1
            pages.append(page)
    return pages


def _fetch_with_bounded_retry(
    pages: list[list[dict[str, int]]],
    profile: BenchmarkProfile,
) -> tuple[list[dict[str, int]], int, int]:
    """Replay pages, failing transiently once per source, with three attempts."""
    by_source: dict[int, list[list[dict[str, int]]]] = {}
    for page in pages:
        by_source.setdefault(page[0]["source_id"], []).append(page)
    listings: list[dict[str, int]] = []
    retries = 0
    pages_fetched = 0
    for source_id in range(profile.sources):
        attempts = 0
        while True:
            attempts += 1
            if attempts <= profile.transient_failures_per_source:
                retries += 1
                if attempts >= 3:
                    raise RuntimeError("synthetic transient retry budget exhausted")
                continue
            source_pages = by_source[source_id]
            pages_fetched += len(source_pages)
            for page in source_pages:
                listings.extend(page)
            break
    return listings, pages_fetched, retries


def _budget_failures(metrics: dict[str, Any], budget: BenchmarkBudget) -> list[str]:
    checks = (
        ("run_window_ms", metrics["wall_time_seconds"] * 1000, budget.run_window_ms),
        ("peak_memory_mb", metrics["peak_memory_bytes"] / (1024 * 1024), budget.peak_memory_mb),
        ("cpu_seconds", metrics["cpu_seconds"], budget.cpu_seconds),
        ("query_count", metrics["query_count"], budget.query_count),
        ("external_calls", metrics["external_calls"], budget.external_calls),
    )
    return [f"{name}={value:g} exceeds {limit:g}" for name, value, limit in checks if value > limit]


def run_benchmark(
    profile: str = "ci",
    *,
    seed: int = 324,
    assert_budgets: bool = False,
) -> dict[str, Any]:
    """Run one generated scenario and return JSON-serializable metrics."""
    selected = get_profile(profile)
    if not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    stages: dict[str, float] = {}
    cpu_start = resource.getrusage(resource.RUSAGE_SELF).ru_utime
    tracemalloc.start()
    started = time.perf_counter()
    try:
        stage_start = time.perf_counter()
        pages = _synthetic_pages(selected, seed)
        stages["generate_ms"] = (time.perf_counter() - stage_start) * 1000

        stage_start = time.perf_counter()
        fetched, pages_fetched, retries = _fetch_with_bounded_retry(pages, selected)
        stages["paginate_retry_ms"] = (time.perf_counter() - stage_start) * 1000

        stage_start = time.perf_counter()
        organizations = {
            organization_id: SimpleNamespace(
                pk=organization_id,
                rto_policy="R",
                funding_round="A",
                industry="Software",
                accelerated_vesting=True,
                source_metadata={"culture": "remote-first"},
                avg_scores=lambda: [],
            )
            for organization_id in range(32)
        }
        listings = [
            _listing(item["listing_id"], organizations[item["organization_id"]])
            for item in fetched
        ]
        resolved = sum(item["organization_id"] in organizations for item in fetched)
        stages["resolve_organizations_ms"] = (time.perf_counter() - stage_start) * 1000

        stage_start = time.perf_counter()
        criteria = project_criteria(_DEFAULT_PREFERENCES, 1)
        ranked = 0
        for _ in range(selected.users):
            ranked += len(rank_listings(listings, criteria, DEFAULT_CONFIG))
        stages["match_users_ms"] = (time.perf_counter() - stage_start) * 1000

        stage_start = time.perf_counter()
        cache = {organization_id: "stale" for organization_id in organizations}
        for organization_id in set(item["organization_id"] for item in fetched):
            cache.pop(organization_id, None)
        stages["invalidate_cache_ms"] = (time.perf_counter() - stage_start) * 1000

        wall_time = time.perf_counter() - started
        cpu_seconds = resource.getrusage(resource.RUSAGE_SELF).ru_utime - cpu_start
        _, peak_memory = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    metrics: dict[str, Any] = {
        "profile": selected.name,
        "seed": seed,
        "stage_timings_ms": stages,
        "sources_total": selected.sources,
        "pages_fetched": pages_fetched,
        "listings_generated": len(fetched),
        "listings_ingested": len(listings),
        "organizations_resolved": resolved,
        "organizations_unresolved": len(fetched) - resolved,
        "users_total": selected.users,
        "matches_ranked": ranked,
        "retries": retries,
        "cache_invalidations": len(organizations) - len(cache),
        "query_count": 0,
        "external_calls": 0,
        "wall_time_seconds": wall_time,
        "cpu_seconds": cpu_seconds,
        "peak_memory_bytes": peak_memory,
        "budget": asdict(selected.budget),
    }
    failures = _budget_failures(metrics, selected.budget)
    metrics["budget_failures"] = failures
    metrics["budget_passed"] = not failures
    if assert_budgets and failures:
        raise AssertionError("benchmark budget exceeded: " + "; ".join(failures))
    return metrics


def metrics_json(metrics: dict[str, Any]) -> str:
    """Encode metrics with stable key ordering for CI artifacts."""
    return json.dumps(metrics, indent=2, sort_keys=True)


__all__ = [
    "BenchmarkBudget",
    "BenchmarkProfile",
    "PROFILES",
    "get_profile",
    "metrics_json",
    "run_benchmark",
]
