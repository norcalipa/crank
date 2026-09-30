# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""What a source's number measures, and which score types it may feed.

Approval to *access* a source does not make its numbers a valid measure of
every quality. A consumer or business star rating (Google Places, Yelp)
measures public reputation; it is not an employee-culture, leadership or
hiring-probability measure. The code map below is the enforcement point and
``docs/source-catalog.yaml`` is checked against it by
``crank/tests/test_source_catalog.py``.

The map fails closed: a score type that is not listed, or whose name mentions
"hiring", accepts no automated measurement.
"""

from __future__ import annotations

from enum import Enum


class MeasurementKind(str, Enum):
    CONSUMER_BUSINESS_RATING = "consumer_business_rating"
    EMPLOYEE_SURVEY = "employee_survey"
    COMPENSATION_BENCHMARK = "compensation_benchmark"
    EMPLOYER_POLICY_STATEMENT = "employer_policy_statement"
    CURATED_REVIEW = "curated_review"
    FINANCIAL_DATA = "financial_data"
    TECH_STACK_LISTING = "tech_stack_listing"


_K = MeasurementKind

SCORE_TYPE_ALLOWED_MEASUREMENTS: dict[str, frozenset[MeasurementKind]] = {
    "Culture": frozenset({_K.EMPLOYEE_SURVEY, _K.CURATED_REVIEW}),
    "Leadership": frozenset({_K.EMPLOYEE_SURVEY, _K.CURATED_REVIEW}),
    "Total Compensation": frozenset({_K.COMPENSATION_BENCHMARK, _K.CURATED_REVIEW}),
    "Reputation": frozenset({_K.CONSUMER_BUSINESS_RATING, _K.CURATED_REVIEW}),
    "Product and Mission": frozenset({_K.CURATED_REVIEW}),
    "Tech Stack": frozenset({_K.TECH_STACK_LISTING, _K.CURATED_REVIEW}),
    "Lifecycle and Financials": frozenset(
        {_K.FINANCIAL_DATA, _K.COMPENSATION_BENCHMARK, _K.CURATED_REVIEW}
    ),
    "RTO and Work Hours": frozenset(
        {_K.EMPLOYER_POLICY_STATEMENT, _K.EMPLOYEE_SURVEY, _K.CURATED_REVIEW}
    ),
}


def coerce_kind(kind) -> MeasurementKind | None:
    """Return ``kind`` as a :class:`MeasurementKind`, or ``None`` if invalid."""
    try:
        return MeasurementKind(kind)
    except ValueError:
        return None


def measurement_allows(kind, score_type_name: str) -> bool:
    """True when a source measuring ``kind`` may feed ``score_type_name``."""
    resolved = coerce_kind(kind)
    name = str(score_type_name or "")
    if resolved is None or "hiring" in name.lower():
        return False
    return resolved in SCORE_TYPE_ALLOWED_MEASUREMENTS.get(name, frozenset())


__all__ = [
    "MeasurementKind",
    "SCORE_TYPE_ALLOWED_MEASUREMENTS",
    "coerce_kind",
    "measurement_allows",
]
