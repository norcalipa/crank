# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Schema-validated result types for the job-search orchestration service.

Model output is validated against a strict schema before it is allowed to
reach the preference service or persistence. Only :class:`AssistantCompletion`
instances created through :meth:`AssistantCompletion.from_json` may be passed
forward.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from crank.agents.job_search.errors import InvalidModelOutputError

#: Top-level keys that must all be present in the model's result object.
_REQUIRED_KEYS = frozenset(
    {"message", "cited_organization_ids", "cited_job_listing_ids", "preference_patch"}
)
_ALLOWED_KEYS = _REQUIRED_KEYS
#: Absolute ceiling on how many cited organization IDs are accepted.
_MAX_CITED_ORGANIZATIONS = 200
#: Absolute ceiling on how many cited job-listing IDs are accepted.
_MAX_CITED_JOB_LISTINGS = 200
#: Ceiling on preference-patch nesting depth (guards against pathological JSON).
_MAX_PATCH_DEPTH = 8
# Keep a hostile provider response from becoming an unbounded in-memory object
# before the preference service gets a chance to validate it.
_MAX_MESSAGE_LENGTH = 8000
_MAX_PATCH_KEYS = 200
_MAX_PATCH_SEQUENCE_LENGTH = 200
_MAX_PATCH_STRING_LENGTH = 2000
_MAX_PATCH_JSON_BYTES = 16 * 1024
#: Maximum number of job/org result entries the server will return.
_MAX_RESULT_ENTRIES = 50
#: Maximum serialized size (bytes) of the results block.
_MAX_RESULTS_JSON_BYTES = 64 * 1024


@dataclass(frozen=True)
class JobResult:
    """A single job-listing result card derived from server-controlled data.

    All fields come from the bounded tool output, never from model-invented
    content.  The canonical URL is always the server-controlled value.
    """

    id: int
    title: str
    organization_name: str
    location: str
    remote: bool
    compensation: Optional[Dict[str, Any]] = None
    canonical_url: str = ""
    observed_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class OrganizationResult:
    """A single organization result card derived from server-controlled data."""

    id: int
    name: str
    url: str = ""
    funding_round: str = ""
    rto_policy: str = ""


@dataclass(frozen=True)
class StructuredResults:
    """Citation-validated structured results attached to an assistant turn.

    ``jobs`` and ``organizations`` contain only entries whose IDs appear in
    the corresponding cited-id lists and were returned by the server-controlled
    tools.  Model-invented URLs or data never appear here.
    """

    jobs: Tuple[JobResult, ...] = ()
    organizations: Tuple[OrganizationResult, ...] = ()

    def to_json_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dict suitable for persistence and transport."""
        return {
            "jobs": [
                {
                    "id": j.id,
                    "title": j.title,
                    "organization_name": j.organization_name,
                    "location": j.location,
                    "remote": j.remote,
                    "compensation": j.compensation,
                    "canonical_url": j.canonical_url,
                    "observed_at": j.observed_at,
                    "updated_at": j.updated_at,
                }
                for j in self.jobs
            ],
            "organizations": [
                {
                    "id": o.id,
                    "name": o.name,
                    "url": o.url,
                    "funding_round": o.funding_round,
                    "rto_policy": o.rto_policy,
                }
                for o in self.organizations
            ],
        }

    @classmethod
    def from_json_dict(cls, raw: Any) -> "StructuredResults":
        """Reconstruct from a persisted/transport JSON dict.

        Raises :class:`InvalidModelOutputError` for malformed shapes so bad
        persisted data surfaces as a typed error, not a crash.
        """
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise InvalidModelOutputError("results block must be a dict or null")
        jobs_raw = raw.get("jobs", [])
        orgs_raw = raw.get("organizations", [])
        if not isinstance(jobs_raw, list):
            raise InvalidModelOutputError("results.jobs must be a list")
        if not isinstance(orgs_raw, list):
            raise InvalidModelOutputError("results.organizations must be a list")
        if len(jobs_raw) > _MAX_RESULT_ENTRIES:
            raise InvalidModelOutputError(
                "results.jobs exceeds %d entries" % _MAX_RESULT_ENTRIES
            )
        if len(orgs_raw) > _MAX_RESULT_ENTRIES:
            raise InvalidModelOutputError(
                "results.organizations exceeds %d entries" % _MAX_RESULT_ENTRIES
            )
        jobs: List[JobResult] = []
        for entry in jobs_raw:
            if not isinstance(entry, dict):
                raise InvalidModelOutputError("each job result must be a dict")
            jobs.append(
                JobResult(
                    id=_req_int(entry, "id", "job"),
                    title=_req_str(entry, "title", "job"),
                    organization_name=str(entry.get("organization_name", "")),
                    location=str(entry.get("location", "")),
                    remote=bool(entry.get("remote", False)),
                    compensation=entry.get("compensation"),
                    canonical_url=str(entry.get("canonical_url", "")),
                    observed_at=entry.get("observed_at"),
                    updated_at=entry.get("updated_at"),
                )
            )
        orgs: List[OrganizationResult] = []
        for entry in orgs_raw:
            if not isinstance(entry, dict):
                raise InvalidModelOutputError("each organization result must be a dict")
            orgs.append(
                OrganizationResult(
                    id=_req_int(entry, "id", "organization"),
                    name=_req_str(entry, "name", "organization"),
                    url=str(entry.get("url", "")),
                    funding_round=str(entry.get("funding_round", "")),
                    rto_policy=str(entry.get("rto_policy", "")),
                )
            )
        return cls(jobs=tuple(jobs), organizations=tuple(orgs))


@dataclass(frozen=True)
class AssistantCompletion:
    """A schema-validated assistant turn.

    Attributes
    ----------
    message:
        The human-readable assistant reply.
    cited_organization_ids:
        Unique, ordered organization IDs the recommendation relies on. These are
        validated downstream against the server-controlled catalog.
    cited_job_listing_ids:
        Unique, ordered job-listing IDs the reply references. These are
        validated downstream against the server-controlled listing tools.
    preference_patch:
        Optional typed preference update forwarded to the preference service.
    """

    message: str
    cited_organization_ids: tuple[int, ...] = ()
    cited_job_listing_ids: tuple[int, ...] = ()
    preference_patch: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, raw: Any) -> AssistantCompletion:
        """Validate ``raw`` and return an :class:`AssistantCompletion`.

        Accepts a parsed dict or a JSON string. Raises
        :class:`InvalidModelOutputError` when the shape is wrong, non-serializable,
        or otherwise fails the schema gate.
        """
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                raise InvalidModelOutputError(
                    "model output is not valid JSON"
                ) from exc
        else:
            payload = raw

        if not isinstance(payload, dict):
            raise InvalidModelOutputError(
                "model output must be a JSON object"
            )

        missing = _REQUIRED_KEYS - set(payload.keys())
        if missing:
            raise InvalidModelOutputError(
                f"model output is missing required keys: {', '.join(sorted(missing))}"
            )
        unknown = set(payload.keys()) - _ALLOWED_KEYS
        if unknown:
            raise InvalidModelOutputError(
                f"model output contains unknown keys: {', '.join(sorted(str(key) for key in unknown))}"
            )

        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            raise InvalidModelOutputError(
                "model output 'message' must be a non-empty string"
            )
        if len(message) > _MAX_MESSAGE_LENGTH:
            raise InvalidModelOutputError(
                f"model output 'message' exceeds {_MAX_MESSAGE_LENGTH} characters"
            )

        raw_ids = payload.get("cited_organization_ids")
        if not isinstance(raw_ids, (list, tuple)):
            raise InvalidModelOutputError(
                "model output 'cited_organization_ids' must be a list"
            )
        if len(raw_ids) > _MAX_CITED_ORGANIZATIONS:
            raise InvalidModelOutputError(
                f"model output cites more than {_MAX_CITED_ORGANIZATIONS} organization IDs"
            )
        org_ids: list[int] = []
        for value in raw_ids:
            # bool is a subclass of int; exclude it so "true" cannot smuggle in.
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidModelOutputError(
                    "model output cited_organization_ids must be integers"
                )
            org_ids.append(value)
        if len(set(org_ids)) != len(org_ids):
            raise InvalidModelOutputError(
                "model output cited_organization_ids must be unique"
            )

        raw_listing_ids = payload.get("cited_job_listing_ids")
        if not isinstance(raw_listing_ids, (list, tuple)):
            raise InvalidModelOutputError(
                "model output 'cited_job_listing_ids' must be a list"
            )
        if len(raw_listing_ids) > _MAX_CITED_JOB_LISTINGS:
            raise InvalidModelOutputError(
                f"model output cites more than {_MAX_CITED_JOB_LISTINGS} job listing IDs"
            )
        listing_ids: list[int] = []
        for value in raw_listing_ids:
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidModelOutputError(
                    "model output cited_job_listing_ids must be integers"
                )
            listing_ids.append(value)
        if len(set(listing_ids)) != len(listing_ids):
            raise InvalidModelOutputError(
                "model output cited_job_listing_ids must be unique"
            )

        patch = payload.get("preference_patch")
        if patch is not None:
            if not isinstance(patch, dict):
                raise InvalidModelOutputError(
                    "model output preference_patch must be an object or null"
                )
            _assert_bounded_patch(patch)
            patch_bytes = len(json.dumps(patch, separators=(",", ":")).encode("utf-8"))
            if patch_bytes > _MAX_PATCH_JSON_BYTES:
                raise InvalidModelOutputError(
                    f"preference_patch exceeds {_MAX_PATCH_JSON_BYTES} bytes"
                )

        return cls(
            message=message.strip(),
            cited_organization_ids=tuple(sorted(org_ids)),
            cited_job_listing_ids=tuple(sorted(listing_ids)),
            preference_patch=_freeze_patch(patch) if patch is not None else None,
        )

    @property
    def has_preference_patch(self) -> bool:
        return self.preference_patch is not None


def _assert_bounded_scalar(value: Any) -> None:
    """Reject leaf values that are not plain JSON-serializable scalars.

    Booleans are deliberately rejected even though they are JSON-serializable,
    because patch leaves are typed configuration values (numbers/strings) and
    bools are excluded here consistently (as in ``cited_organization_ids``).
    Dicts/lists are handled by the recursive walkers, so this is only called
    for scalar leaves.
    """
    if isinstance(value, bool) or (
        value is not None and not isinstance(value, (str, int, float))
    ):
        raise InvalidModelOutputError(
            "preference_patch values must be strings, numbers, or nested "
            "objects/lists; booleans and other types are not allowed"
        )
    if isinstance(value, str) and len(value) > _MAX_PATCH_STRING_LENGTH:
        raise InvalidModelOutputError(
            f"preference_patch strings exceed {_MAX_PATCH_STRING_LENGTH} characters"
        )


def _assert_bounded_patch(patch: dict[str, Any], _depth: int = 0) -> None:
    """Recursively enforce the patch is JSON-serializable, typed, and bounded."""
    if _depth > _MAX_PATCH_DEPTH:
        raise InvalidModelOutputError("preference_patch is nested too deeply")
    if len(patch) > _MAX_PATCH_KEYS:
        raise InvalidModelOutputError(
            f"preference_patch contains more than {_MAX_PATCH_KEYS} keys"
        )
    for key, value in patch.items():
        if not isinstance(key, str):
            raise InvalidModelOutputError(
                "preference_patch keys must be strings"
            )
        if isinstance(value, dict):
            _assert_bounded_patch(value, _depth + 1)
        elif isinstance(value, (list, tuple)):
            _assert_bounded_sequence(value, _depth + 1)
        else:
            # MAJOR-1: scalar leaves must be type-checked too (mirrors the
            # sequence path) so an un-serializable or boolean leaf cannot
            # smuggle through the patch object.
            _assert_bounded_scalar(value)


def _assert_bounded_sequence(seq: list[Any], _depth: int) -> None:
    if _depth > _MAX_PATCH_DEPTH:
        raise InvalidModelOutputError("preference_patch is nested too deeply")
    if len(seq) > _MAX_PATCH_SEQUENCE_LENGTH:
        raise InvalidModelOutputError(
            f"preference_patch sequences contain more than {_MAX_PATCH_SEQUENCE_LENGTH} items"
        )
    for value in seq:
        if isinstance(value, dict):
            _assert_bounded_patch(value, _depth + 1)
        elif isinstance(value, (list, tuple)):
            _assert_bounded_sequence(value, _depth + 1)
        else:
            # MAJOR-2: booleans are rejected in sequences; now rejected in
            # dicts identically (consistent policy) with a correct message.
            _assert_bounded_scalar(value)


def _freeze_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Return the patch as plain JSON-serializable data (no Dataclass/etc.)."""
    return json.loads(json.dumps(patch))


def _req_int(entry: Dict[str, Any], key: str, label: str) -> int:
    """Extract a required integer from *entry*, raising InvalidModelOutputError."""
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidModelOutputError(
            "results %s entry %r must be an integer" % (label, key)
        )
    return value


def _req_str(entry: Dict[str, Any], key: str, label: str) -> str:
    """Extract a required string from *entry*, raising InvalidModelOutputError."""
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidModelOutputError(
            "results %s entry %r must be a non-empty string" % (label, key)
        )
    return value
