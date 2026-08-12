# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Orchestration for bounded, independently committed score collection.

The service deliberately keeps source work outside one transaction.  A bad
adapter, malformed feed, or persistence error finalizes only that source's
``SourceRun``; successful sources remain committed and available for later
replays.  The enclosing ``AgentRun`` is failed only when no source succeeded.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from crank.agents.sources.config import default_resolution_config
from crank.agents.sources.contract import SourceQuery
from crank.agents.sources.normalize import ScoreNormalizer
from crank.agents.sources.registry import build_adapter
from crank.models.agent_run import AgentRun
from crank.models.source import ApprovalState, SourceCatalog, SourceRun
from crank.services import agent_runs
from crank.services import monitoring
from crank.services.scores import NOOP, persist_score_observation

logger = logging.getLogger("score_gathering")

COUNT_KEYS = (
    "sources_total",
    "sources_succeeded",
    "sources_failed",
    "observations_fetched",
    "observations_normalized",
    "observations_persisted",
    "observations_unresolved",
    "observations_rejected",
    "duplicates_skipped",
)


class ScoreGatheringError(RuntimeError):
    """Raised when no source completed successfully."""

    def __init__(self, message, counts):
        super().__init__(message)
        self.counts = dict(counts)


@dataclass(frozen=True)
class _FetchedSource:
    adapter: object
    result: object


def _empty_counts(total=0):
    counts = {key: 0 for key in COUNT_KEYS}
    counts["sources_total"] = total
    return counts


def _query_from_options(options, max_observations):
    query = options.get("query")
    if query is None:
        return SourceQuery(max_observations=max_observations)
    if isinstance(query, SourceQuery):
        return query
    if isinstance(query, dict):
        values = dict(query)
        values.setdefault("max_observations", max_observations)
        return SourceQuery(**values)
    raise TypeError("query must be a SourceQuery or mapping")


def _fetch_source(source, query, timeout_seconds):
    """Build and fetch one adapter, enforcing the source wall-clock budget."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="score-source")
    future = executor.submit(_build_and_fetch, source, query)
    try:
        adapter, result = future.result(timeout=max(0.001, timeout_seconds))
        return _FetchedSource(adapter=adapter, result=result)
    except TimeoutError as exc:
        future.cancel()
        raise TimeoutError(
            f"source timeout after {timeout_seconds:g} seconds"
        ) from exc
    finally:
        # A running network call is expected to have its own bounded transport
        # timeout.  Do not wait here after a source deadline has elapsed.
        executor.shutdown(wait=False, cancel_futures=True)


def _build_and_fetch(source, query):
    adapter = build_adapter(source)
    return adapter, adapter.fetch(query)


def _result_observations(result):
    if hasattr(result, "observations"):
        return list(result.observations)
    return list(result or ())


def _as_normalizer_observation(observation, *, source, config, run):
    """Adapt either supported source-contract observation shape.

    The source adapter contract uses ``target_identity``/``range_low`` while
    the normalizer contract uses ``target``/``low``/``high``.  Keeping this
    conversion at the orchestration boundary lets both contracts remain
    independently testable and prevents a raw adapter object from reaching the
    persistence layer.
    """
    if hasattr(observation, "external_type") and hasattr(observation, "target"):
        return observation

    from crank.agents.sources.types import RawScoreObservation

    return RawScoreObservation(
        source_key=config.source_key,
        external_type=str(observation.score_type),
        target=str(observation.target_identity),
        value=str(observation.value),
        low=str(observation.range_low),
        high=str(observation.range_high),
        source_url=str(observation.source_url),
        external_source_id=str(observation.external_id),
        observed_at=observation.observed_at,
        fetched_at=observation.fetched_at,
        adapter=str(getattr(observation, "adapter", source.adapter_key)),
        adapter_version=str(observation.adapter_version),
        run_correlation_id=str(
            getattr(observation, "run_correlation_id", None)
            or run.correlation_id
        ),
    )


def _provenance(observation):
    return {
        "external_id": observation.external_source_id,
        "source_url": observation.source_url,
        "adapter_version": observation.adapter_version,
        "observed_at": (
            observation.observed_at.isoformat()
            if observation.observed_at
            else None
        ),
        "fetched_at": (
            observation.fetched_at.isoformat()
            if observation.fetched_at
            else None
        ),
        "raw_value": observation.raw_value,
        "raw_low": observation.raw_low,
        "raw_high": observation.raw_high,
        "normalized_value": (
            str(observation.value) if observation.value is not None else None
        ),
    }


def _add_counts(target, source_counts):
    for key in COUNT_KEYS:
        if key != "sources_total":
            target[key] += int(source_counts.get(key, 0))


def _finalize_deadline_source(source, run, reason, now=None):
    started = now or timezone.now()
    source_run = SourceRun.objects.create(
        source=source,
        agent_run=run,
        status=AgentRun.Status.RUNNING,
        started_at=started,
    )
    source_run.finalize(
        AgentRun.Status.FAILED,
        counts={},
        error_summary=reason,
    )
    logger.warning(
        "score source not processed: run_correlation_id=%s source_run_id=%s "
        "source_id=%s reason=%s",
        run.correlation_id,
        source_run.pk,
        source.pk,
        reason,
    )


def gather_scores(run, **options):
    """Gather, normalize, and persist scores for approved/enabled sources.

    Returns the stable aggregate counter schema used by ``AgentRun.counts``.
    Source failures are isolated.  A run with at least one successful source is
    successful even when other sources fail; a non-empty catalog with no
    successful source raises :class:`ScoreGatheringError` after finalizing each
    source segment.
    """
    sources = list(
        SourceCatalog.objects.filter(
            approval_state=ApprovalState.APPROVED,
            enabled=True,
        ).order_by("pk")
    )
    counts = _empty_counts(len(sources))
    if not sources:
        logger.info(
            "score gathering no sources: run_correlation_id=%s",
            run.correlation_id,
        )
        return counts

    config = options.get("resolution_config") or default_resolution_config()
    query = _query_from_options(options, config.max_observations)
    deadline_seconds = float(
        options.get(
            "deadline_seconds",
            getattr(settings, "GATHER_SCORES_DEADLINE_SECONDS", 300),
        )
    )
    source_timeout = float(
        options.get(
            "source_timeout_seconds",
            getattr(settings, "GATHER_SCORES_SOURCE_TIMEOUT_SECONDS", 120),
        )
    )
    deadline = time.monotonic() + max(0.0, deadline_seconds)
    successful_sources = 0

    for index, source in enumerate(sources):
        if time.monotonic() >= deadline:
            remaining = sources[index:]
            for remaining_source in remaining:
                counts["sources_failed"] += 1
                _finalize_deadline_source(
                    remaining_source,
                    run,
                    "gathering deadline exceeded before source started",
                )
            break

        source_started = timezone.now()
        previous_success = source.last_success_at
        freshness_seconds = (
            max(0, int((source_started - previous_success).total_seconds()))
            if previous_success
            else 0
        )
        source_run = SourceRun.objects.create(
            source=source,
            agent_run=run,
            status=AgentRun.Status.RUNNING,
            started_at=source_started,
        )
        source_counts = {key: 0 for key in COUNT_KEYS}
        try:
            fetched = _fetch_source(
                source,
                query,
                min(
                    source_timeout,
                    float(getattr(source, "timeout_seconds", source_timeout)),
                    max(0.001, deadline - time.monotonic()),
                ),
            )
            raw_observations = _result_observations(fetched.result)
            source_counts["observations_fetched"] = len(raw_observations)
            normalizer = ScoreNormalizer(config)
            normalized_input = [
                _as_normalizer_observation(
                    observation, source=source, config=config, run=run
                )
                for observation in raw_observations
            ]
            report = normalizer.normalize(normalized_input)
            source_counts["observations_normalized"] = int(report.normalized)
            source_counts["observations_unresolved"] = int(report.unresolved)
            source_counts["observations_rejected"] = int(report.rejected)
            source_counts["duplicates_skipped"] = int(report.duplicates_skipped)

            for outcome in report.outcomes:
                normalized = outcome.observation
                if normalized is None:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("gathering deadline exceeded during persistence")
                if normalized.value is None:
                    # A range without a point value cannot be represented by the
                    # Score model; treat it as a rejected persistence input.
                    source_counts["observations_rejected"] += 1
                    source_counts["observations_normalized"] -= 1
                    continue
                result = persist_score_observation(
                    source=normalized.source_id,
                    target=normalized.target_id,
                    score_type=normalized.type_id,
                    value=float(normalized.value),
                    low_threshold=(
                        float(normalized.low_threshold)
                        if normalized.low_threshold is not None
                        else None
                    ),
                    high_threshold=(
                        float(normalized.high_threshold)
                        if normalized.high_threshold is not None
                        else None
                    ),
                    provenance=_provenance(normalized),
                    run=run,
                )
                if result.outcome == NOOP:
                    source_counts["duplicates_skipped"] += 1
                else:
                    source_counts["observations_persisted"] += 1

            source_run.adapter_version = str(getattr(fetched.adapter, "version", ""))
            source_run.save(update_fields=["adapter_version"])
            source_run.finalize(
                AgentRun.Status.SUCCEEDED,
                counts={k: source_counts[k] for k in COUNT_KEYS},
            )
            _add_counts(counts, source_counts)
            counts["sources_succeeded"] += 1
            successful_sources += 1
            monitoring.record_event(
                "source_stage",
                {
                    "status": "succeeded",
                    "stage": "score_gathering",
                    "source_key": source.adapter_key,
                    "freshness_seconds": freshness_seconds,
                    "items_succeeded": source_counts["observations_persisted"],
                    "items_failed": source_counts["observations_rejected"],
                },
            )
            logger.info(
                "score source succeeded: run_correlation_id=%s source_run_id=%s "
                "source_id=%s counts=%s",
                run.correlation_id,
                source_run.pk,
                source.pk,
                source_counts,
            )
        except Exception as exc:  # noqa: BLE001 - isolate each source
            _add_counts(counts, source_counts)
            source_run.finalize(
                AgentRun.Status.FAILED,
                counts={k: source_counts[k] for k in COUNT_KEYS},
                error_summary=agent_runs.sanitize_error(exc),
            )
            counts["sources_failed"] += 1
            monitoring.record_event(
                "source_stage",
                {
                    "status": "failed",
                    "stage": "score_gathering",
                    "source_key": source.adapter_key,
                    "freshness_seconds": freshness_seconds,
                    "reason_code": monitoring.failure_reason(exc),
                    "items_failed": source_counts["observations_rejected"],
                },
            )
            logger.warning(
                "score source failed: run_correlation_id=%s source_run_id=%s "
                "source_id=%s error_summary=%s counts=%s",
                run.correlation_id,
                source_run.pk,
                source.pk,
                agent_runs.sanitize_error(exc),
                source_counts,
            )

    if counts["sources_total"] and not successful_sources:
        raise ScoreGatheringError(
            "all approved and enabled score sources failed", counts
        )
    return counts


__all__ = ["COUNT_KEYS", "ScoreGatheringError", "gather_scores"]
