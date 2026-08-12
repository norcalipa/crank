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
from typing import Any, Dict, List, Optional, Tuple

from crank.agents.job_search.errors import InvalidModelOutputError

#: Top-level keys that must all be present in the model's result object.
_REQUIRED_KEYS = frozenset({"message", "cited_organization_ids", "preference_patch"})
_ALLOWED_KEYS = _REQUIRED_KEYS
#: Absolute ceiling on how many cited organization IDs are accepted.
_MAX_CITED_ORGANIZATIONS = 200
#: Ceiling on preference-patch nesting depth (guards against pathological JSON).
_MAX_PATCH_DEPTH = 8
# Keep a hostile provider response from becoming an unbounded in-memory object
# before the preference service gets a chance to validate it.
_MAX_MESSAGE_LENGTH = 8000
_MAX_PATCH_KEYS = 200
_MAX_PATCH_SEQUENCE_LENGTH = 200
_MAX_PATCH_STRING_LENGTH = 2000
_MAX_PATCH_JSON_BYTES = 16 * 1024


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
    preference_patch:
        Optional typed preference update forwarded to the preference service.
    """

    message: str
    cited_organization_ids: Tuple[int, ...] = ()
    preference_patch: Optional[Dict[str, Any]] = None

    @classmethod
    def from_json(cls, raw: Any) -> "AssistantCompletion":
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
                "model output is missing required keys: %s"
                % ", ".join(sorted(missing))
            )
        unknown = set(payload.keys()) - _ALLOWED_KEYS
        if unknown:
            raise InvalidModelOutputError(
                "model output contains unknown keys: %s"
                % ", ".join(sorted(str(key) for key in unknown))
            )

        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            raise InvalidModelOutputError(
                "model output 'message' must be a non-empty string"
            )
        if len(message) > _MAX_MESSAGE_LENGTH:
            raise InvalidModelOutputError(
                "model output 'message' exceeds %d characters"
                % _MAX_MESSAGE_LENGTH
            )

        raw_ids = payload.get("cited_organization_ids")
        if not isinstance(raw_ids, (list, tuple)):
            raise InvalidModelOutputError(
                "model output 'cited_organization_ids' must be a list"
            )
        if len(raw_ids) > _MAX_CITED_ORGANIZATIONS:
            raise InvalidModelOutputError(
                "model output cites more than %d organization IDs"
                % _MAX_CITED_ORGANIZATIONS
            )
        org_ids: List[int] = []
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
                    "preference_patch exceeds %d bytes" % _MAX_PATCH_JSON_BYTES
                )

        return cls(
            message=message.strip(),
            cited_organization_ids=tuple(sorted(org_ids)),
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
            "preference_patch strings exceed %d characters"
            % _MAX_PATCH_STRING_LENGTH
        )


def _assert_bounded_patch(patch: Dict[str, Any], _depth: int = 0) -> None:
    """Recursively enforce the patch is JSON-serializable, typed, and bounded."""
    if _depth > _MAX_PATCH_DEPTH:
        raise InvalidModelOutputError("preference_patch is nested too deeply")
    if len(patch) > _MAX_PATCH_KEYS:
        raise InvalidModelOutputError(
            "preference_patch contains more than %d keys" % _MAX_PATCH_KEYS
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


def _assert_bounded_sequence(seq: List[Any], _depth: int) -> None:
    if _depth > _MAX_PATCH_DEPTH:
        raise InvalidModelOutputError("preference_patch is nested too deeply")
    if len(seq) > _MAX_PATCH_SEQUENCE_LENGTH:
        raise InvalidModelOutputError(
            "preference_patch sequences contain more than %d items"
            % _MAX_PATCH_SEQUENCE_LENGTH
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


def _freeze_patch(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Return the patch as plain JSON-serializable data (no Dataclass/etc.)."""
    return json.loads(json.dumps(patch))
