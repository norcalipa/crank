# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Offline capacity and resilience checks for the synthetic job benchmark."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from crank.services import job_pipeline_benchmark as benchmark


def test_ci_profile_is_deterministic_and_within_budget():
    first = benchmark.run_benchmark("ci", seed=324, assert_budgets=True)
    second = benchmark.run_benchmark("ci", seed=324, assert_budgets=True)

    assert first["sources_total"] == 2
    assert first["pages_fetched"] == 4
    assert first["listings_generated"] == 200
    assert first["organizations_unresolved"] == 0
    assert first["users_total"] == 4
    assert first["matches_ranked"] == 800
    assert first["retries"] == 2
    assert first["cache_invalidations"] == 32
    assert first["query_count"] == 0
    assert first["external_calls"] == 0
    assert first["budget_passed"] is True
    assert first["budget_failures"] == []
    assert first["stage_timings_ms"].keys() == {
        "generate_ms",
        "paginate_retry_ms",
        "resolve_organizations_ms",
        "match_users_ms",
        "invalidate_cache_ms",
    }
    # Wall-clock and allocation timings are machine-dependent; generated
    # counts and stage names are the stable replay contract.
    for key in (
        "sources_total",
        "pages_fetched",
        "listings_generated",
        "organizations_resolved",
        "users_total",
        "matches_ranked",
        "retries",
        "cache_invalidations",
    ):
        assert first[key] == second[key]


def test_staging_profile_exercises_large_listing_set():
    result = benchmark.run_benchmark("staging", seed=7)

    assert result["sources_total"] == 3
    assert result["pages_fetched"] == 9
    assert result["listings_generated"] == 900
    assert result["users_total"] == 10
    assert result["matches_ranked"] == 9000
    assert result["retries"] == 3
    assert result["budget_passed"] is True


def test_profile_and_seed_validation():
    assert benchmark.get_profile("CI") == benchmark.PROFILES["ci"]
    with pytest.raises(ValueError, match="unknown benchmark profile"):
        benchmark.get_profile("live")
    with pytest.raises(ValueError, match="non-negative"):
        benchmark.run_benchmark("ci", seed=-1)
    with pytest.raises(ValueError, match="non-negative"):
        benchmark.run_benchmark("ci", seed=1.5)


def test_transient_retry_budget_is_bounded():
    profile = replace(benchmark.PROFILES["ci"], sources=1, transient_failures_per_source=3)
    pages = benchmark._synthetic_pages(profile, 1)

    with pytest.raises(RuntimeError, match="retry budget exhausted"):
        benchmark._fetch_with_bounded_retry(pages, profile)


def test_budget_failure_report_and_assertion():
    metrics = {
        "wall_time_seconds": 2,
        "peak_memory_bytes": 2 * 1024 * 1024,
        "cpu_seconds": 2,
        "query_count": 2,
        "external_calls": 2,
    }
    budget = benchmark.BenchmarkBudget(
        run_window_ms=1,
        peak_memory_mb=1,
        cpu_seconds=1,
        query_count=1,
        external_calls=1,
    )
    failures = benchmark._budget_failures(metrics, budget)
    assert len(failures) == 5
    assert all("exceeds" in failure for failure in failures)
    with patch.object(benchmark, "_budget_failures", return_value=["synthetic failure"]):
        with pytest.raises(AssertionError, match="synthetic failure"):
            benchmark.run_benchmark("ci", assert_budgets=True)


def test_metrics_json_is_stable_json():
    encoded = benchmark.metrics_json({"z": 1, "a": {"b": 2}})
    assert json.loads(encoded) == {"a": {"b": 2}, "z": 1}
    assert encoded.index('"a"') < encoded.index('"z"')


def test_management_command_prints_metrics_and_asserts_budgets(capsys):
    call_command(
        "benchmark_job_pipeline",
        profile="ci",
        seed=324,
        assert_budgets=True,
    )
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["profile"] == "ci"
    assert payload["budget_passed"] is True


def test_management_command_converts_benchmark_errors_to_command_error():
    with pytest.raises(CommandError, match="non-negative"):
        call_command("benchmark_job_pipeline", profile="ci", seed=-1)
