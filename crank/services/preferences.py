# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Validated preference lifecycle services.

This module owns the version-1 preference schema, typed patch semantics,
deterministic markdown projection, and owner-scoped application services
(create-on-first-interaction, read, export, reset, delete, patch).

Design rules from the issue:

* Unknown fields and ambiguous operations fail validation: a patch may
  only reference fields this schema version knows (strict-patch rule).
* Forward compatibility during rolling deploys (epic #454 additive
  rollouts): a stored document may carry additive fields written by a
  newer schema version. ``read``/``export`` serve such documents
  unchanged; ``apply_patch``/``reset`` validate and rewrite only the fields
  this version knows and preserve the unknown portion verbatim — never
  validating, modifying, or dropping it, and never projecting it into
  markdown. A patch that targets an unknown field is still rejected, so
  an old pod can never corrupt a newer pod's fields.
* Markdown is derived output and is never accepted as canonical state.
* Concurrent updates must not silently overwrite a newer preference version,
  enforced with a row lock (``select_for_update``) plus an optimistic
  ``modified`` timestamp check that also works on backends where FOR UPDATE is
  a no-op (e.g. SQLite).
* Preference contents are never written to logs; audit rows store metadata only.
"""
import copy
import logging
from datetime import timezone as _dt_tz

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

from crank.models.preference import (
    SCHEMA_VERSION,
    UserPreference,
    UserPreferenceAudit,
    default_preferences,
)

# Field-size and collection caps. Values at or beyond these limits are rejected.
MAX_SCALAR_LENGTH = 100      # single string field / list item length
MAX_NOTES_LENGTH = 2000      # free-form notes
MAX_LIST_LENGTH = 200        # items in a str_list
MAX_PRIORITIES = 50          # keys in the priorities map
MIN_PRIORITY = 0.0
MAX_PRIORITY = 1.0


class PreferenceError(Exception):
    """Base class for preference lifecycle errors."""


class UnknownFieldError(PreferenceError):
    """A patch referenced a field that does not exist in the schema."""


class InvalidValueError(PreferenceError):
    """A value did not conform to the schema for its field."""


class AmbiguousPatchError(PreferenceError):
    """A patch operation was ambiguous or malformed."""


class StalePreferenceError(PreferenceError):
    """The preference changed since the caller's last read.

    ``current_revision`` carries the revision observed at rejection time (when
    the revision precondition ran) so the caller can offer a fresh review path
    without a second read. It is ``None`` for timestamp-based rejections.
    """

    def __init__(self, message="", *, current_revision=None):
        super().__init__(message)
        self.current_revision = current_revision


#: Sentinel ``expected_modified`` value meaning "no preference row existed
#: at turn start" (issue #487). Passing it to :func:`apply_patch_to_user`
#: applies the patch only if the row is *still* absent at commit time; a row
#: that appeared (or was re-created) mid-turn makes the patch stale instead of
#: overwriting it. Distinct from ``None`` (the explicit low-level opt-out of
#: the check) and from a ``datetime`` (the row existed and must be unchanged).
PREFERENCE_ABSENT = object()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# Leaf type codes:
#   'int'       -- integer (True/False rejected), None allowed
#   'float'     -- int or float (True/False rejected), None allowed
#   'bool'      -- bool, None allowed
#   'str'       -- non-empty string
#   'str_list'  -- list of non-empty strings
#   'float_map' -- dict of string -> 0.0..1.0
_LEAF_TYPES = ("int", "float", "bool", "str", "str_list", "float_map")

_FIELD_SPEC = {
    "compensation": {
        "minimum_salary": "int",
        "currency": "str",
        "equity_minimum_percent": "float",
        "require_public_company": "bool",
        "basis": "str",               # "base" or "total"
        "period": "str",              # "year", "month", or "hour"
        "minimum_total_compensation": "int",
        "equity_liquidity_required": "bool",
        "acceptable_liquidity_events": "str_list",
    },
    "culture": "str_list",  # preferred culture attributes
    "work_location": {
        "modes": "str_list",          # e.g. onsite, hybrid, remote
        "countries": "str_list",
        "require_onsite": "bool",
        "max_in_office_days": "int",  # RTO ceiling (0-7)
        "office_days_exact": "int",   # exact required in-office days (0-7)
    },
    "geography": {
        "regions": "str_list",
        "remote_friendly": "bool",
    },
    "industry": "str_list",
    "funding_stage": "str_list",
    "vesting": {
        "max_cliff_months": "int",
        "max_vesting_months": "int",
        "prefer_accelerated": "bool",
    },
    "exclusions": {
        "companies": "str_list",
        "titles": "str_list",
        "industries": "str_list",
        "locations": "str_list",
    },
    "priorities": "float_map",
    "notes": "str",
    "roles": {
        "families": "str_list",
        "titles": "str_list",
        "seniority": "str_list",
    },
    "importance": "float_map",  # criterion key -> 0.0 (soft) .. 1.0 (hard)
    "scope": {
        "countries": "str_list",
        "role_families": "str_list",
    },
}


# ---------------------------------------------------------------------------
# Criterion support registry (issue #459)
# ---------------------------------------------------------------------------
# Whether the matching engine (crank.agents.jobs.matching.project_criteria)
# actually evaluates a given criterion key. A criterion set by the user but
# registered UNSUPPORTED must be surfaced (see unsupported_criteria) rather
# than silently treated as satisfied.
SUPPORTED = "supported"
UNSUPPORTED = "unsupported"

CRITERION_SUPPORT = {
    "compensation.minimum_salary": SUPPORTED,
    "compensation.currency": SUPPORTED,
    "compensation.equity_minimum_percent": SUPPORTED,
    "compensation.require_public_company": SUPPORTED,
    "compensation.basis": UNSUPPORTED,
    "compensation.period": UNSUPPORTED,
    "culture": SUPPORTED,
    "work_location.modes": SUPPORTED,
    "work_location.countries": SUPPORTED,
    "work_location.require_onsite": UNSUPPORTED,
    "work_location.max_in_office_days": SUPPORTED,
    "geography.regions": SUPPORTED,
    "geography.remote_friendly": SUPPORTED,
    "industry": SUPPORTED,
    "funding_stage": SUPPORTED,
    "vesting.max_cliff_months": SUPPORTED,
    "vesting.max_vesting_months": SUPPORTED,
    "vesting.prefer_accelerated": SUPPORTED,
    "exclusions.companies": SUPPORTED,
    "exclusions.titles": SUPPORTED,
    "exclusions.industries": SUPPORTED,
    "exclusions.locations": SUPPORTED,
    "priorities": SUPPORTED,
    "notes": UNSUPPORTED,
    "roles.families": UNSUPPORTED,
    "roles.titles": UNSUPPORTED,
    "roles.seniority": UNSUPPORTED,
    "compensation.minimum_total_compensation": UNSUPPORTED,
    "compensation.equity_liquidity_required": UNSUPPORTED,
    "compensation.acceptable_liquidity_events": UNSUPPORTED,
    "work_location.office_days_exact": UNSUPPORTED,
    "importance": UNSUPPORTED,
    "scope.countries": UNSUPPORTED,
    "scope.role_families": UNSUPPORTED,
}



# ---------------------------------------------------------------------------
# Pure helpers: schema navigation and validation
# ---------------------------------------------------------------------------
def _split_path(path):
    if not isinstance(path, str) or not path:
        raise UnknownFieldError("Patch paths must be non-empty strings")
    return path.split(".")


def _resolve_spec(path):
    """Return (spec, dynamic) for a dotted path.

    ``spec`` is either a leaf type string or a nested dict node. ``dynamic`` is
    True when the final segment addresses an arbitrary key inside a float_map.
    """
    parts = _split_path(path)
    node = _FIELD_SPEC
    for index, part in enumerate(parts):
        if isinstance(node, dict) and part in node:
            node = node[part]
            continue
        # float_map leaves accept any number of dynamic keys (e.g. priorities.<criterion>).
        if index == len(parts) - 1 and node == "float_map" and parts[:-1]:
            return "float_map_entry", True
        raise UnknownFieldError(f"Unknown preference field: {path!r}")
    if isinstance(node, dict) and node:
        return node, False  # internal node -> subtree to set/reset
    if node in _LEAF_TYPES:
        return node, False
    # Defensive: every non-dict node in _FIELD_SPEC is in _LEAF_TYPES,
    # so this branch is unreachable under the current (flat, single float_map) schema.
    raise UnknownFieldError(f"Unknown preference field: {path!r}")  # pragma: no cover


def _validate_str(value, field, allow_empty=False, max_length=MAX_SCALAR_LENGTH):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise InvalidValueError(
            f"{field!r} must be a non-empty string"
        )
    if len(value) > max_length:
        raise InvalidValueError(
            f"Field {field!r} exceeds maximum length of {max_length}"
        )
    return value


def validate_value(field, leaf_type, value):
    """Validate a single value against a leaf type; return the normalized value."""
    if leaf_type == "bool":
        if value is None:
            return None
        if not isinstance(value, bool):
            raise InvalidValueError(
                f"Field {field!r} must be a boolean"
            )
        return value
    if leaf_type in ("int", "float"):
        if value is None:
            return value
        if isinstance(value, bool):
            raise InvalidValueError(f"Field {field!r} must not be a boolean")
        if leaf_type == "int":
            if not isinstance(value, int):
                raise InvalidValueError(f"Field {field!r} must be an integer")
            if value < 0:
                raise InvalidValueError(f"Field {field!r} must be non-negative")
            if (
                field in ("work_location.max_in_office_days", "work_location.office_days_exact")
                and value > 7
            ):
                raise InvalidValueError(
                    f"Field {field!r} must not exceed 7"
                )
            return value
        if not isinstance(value, (int, float)):
            raise InvalidValueError(
                f"Field {field!r} must be a number"
            )
        return float(value)
    if leaf_type == "str":
        # "notes" is free-form (up to MAX_NOTES_LENGTH and may be empty);
        # other string fields are capped at MAX_SCALAR_LENGTH (M1).
        allow_empty = field == "notes"
        max_length = MAX_NOTES_LENGTH if field == "notes" else MAX_SCALAR_LENGTH
        value = _validate_str(value, field, allow_empty=allow_empty, max_length=max_length)
        if field == "compensation.currency":
            value = value.upper()
            if len(value) != 3 or not value.isascii() or not value.isalpha():
                raise InvalidValueError(
                    f"Field {field!r} must be exactly three ASCII letters"
                )
        elif field == "compensation.basis" and value not in ("base", "total"):
            raise InvalidValueError(
                f"Field {field!r} must be 'base' or 'total'"
            )
        elif field == "compensation.period" and value not in ("year", "month", "hour"):
            raise InvalidValueError(
                f"Field {field!r} must be 'year', 'month', or 'hour'"
            )
        return value
    if leaf_type == "str_list":
        if not isinstance(value, list):
            raise InvalidValueError(f"Field {field!r} must be a list")
        if len(value) > MAX_LIST_LENGTH:
            raise InvalidValueError(
                f"Field {field!r} exceeds {MAX_LIST_LENGTH} items"
            )
        out = []
        for item in value:
            out.append(_validate_str(item, field))
        return out
    if leaf_type == "float_map":
        if not isinstance(value, dict):
            raise InvalidValueError(f"Field {field!r} must be an object")
        if len(value) > MAX_PRIORITIES:
            raise InvalidValueError(
                f"Field {field!r} exceeds {MAX_PRIORITIES} keys"
            )
        out = {}
        for key, weight in value.items():
            _validate_str(key, field)
            if field == "importance" and key not in CRITERION_SUPPORT:
                raise InvalidValueError(
                    f"Unknown criterion key {key!r} for {field!r}"
                )
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise InvalidValueError(
                    f"Priority {key!r} must be a number"
                )
            weight = float(weight)
            if weight < MIN_PRIORITY or weight > MAX_PRIORITY:
                raise InvalidValueError(
                    f"Priority {key!r} must be within {MIN_PRIORITY}-{MAX_PRIORITY}"
                )
            out[key] = weight
        return out
    raise InvalidValueError(f"Unknown leaf type {leaf_type!r}")


def validate_document(document):
    """Validate a full canonical preferences document; raise on any problem.

    Enforces the exact schema shape: all required keys present, no unknown
    fields, and every value type-correct and within caps.
    """
    if not isinstance(document, dict):
        raise InvalidValueError("Preferences must be a JSON object")

    def walk(node_spec, node, prefix):
        if isinstance(node_spec, dict):
            if not isinstance(node, dict):
                raise InvalidValueError(
                    "{!r} must be an object".format(prefix or "<root>")
                )
            missing = [key for key in node_spec if key not in node]
            unknown = [key for key in node if key not in node_spec]
            if missing:
                raise InvalidValueError(
                    "Missing required field(s): {}".format(", ".join(missing))
                )
            if unknown:
                raise UnknownFieldError(
                    "Unknown field(s): {}".format(", ".join(unknown))
                )
            for key, sub_spec in node_spec.items():
                child = ".".join(prefix.split(".") + [key]) if prefix else key
                walk(sub_spec, node[key], child)
            return
        validate_value(prefix, node_spec, node)

    walk(_FIELD_SPEC, document, "")


def _validate_known_fields(document, spec=_FIELD_SPEC, prefix=""):
    """Validate the portion of ``document`` this schema version knows.

    Unlike :func:`validate_document`, unknown (additive) keys at any
    object level are tolerated: a stored document written by a newer
    schema version must remain servable by older code during a rolling
    deploy. Known fields are still strictly required and type-checked, so
    a document missing or corrupting a known field fails exactly as
    before. Callers must preserve the tolerated unknown keys themselves
    (see :func:`_extract_unknown`).
    """
    if not isinstance(document, dict):
        raise InvalidValueError("Preferences must be a JSON object")
    missing = [key for key in spec if key not in document]
    if missing:
        raise InvalidValueError(
            "Missing required field(s): {}".format(", ".join(missing))
        )
    for key, sub_spec in spec.items():
        child = "{}.{}".format(prefix, key) if prefix else key
        if isinstance(sub_spec, dict):
            _validate_known_fields(document[key], sub_spec, child)
        else:
            validate_value(child, sub_spec, document[key])


def _extract_unknown(document, spec=_FIELD_SPEC):
    """Deep-copy the additive (unknown-to-this-schema) part of a document.

    Unknown keys are collected at every object level: a new top-level
    section (e.g. a future ``notifications``) and new keys inside known
    sections (e.g. a future ``compensation.new_field``) are both captured.
    Values are copied verbatim and never validated — this schema version
    treats them as opaque payloads owned by a newer version.
    """
    unknown = {}
    if not isinstance(document, dict) or not isinstance(spec, dict):
        return unknown
    for key, value in document.items():
        if key not in spec:
            unknown[key] = copy.deepcopy(value)
        elif isinstance(spec[key], dict) and isinstance(value, dict):
            nested = _extract_unknown(value, spec[key])
            if nested:
                unknown[key] = nested
    return unknown


def _merge_unknown(document, unknown):
    """Reattach the additive portion captured by :func:`_extract_unknown`."""
    for key, value in unknown.items():
        if (
            key in document
            and isinstance(document[key], dict)
            and isinstance(value, dict)
        ):
            _merge_unknown(document[key], value)
        else:
            document[key] = value
    return document


def _fill_missing_defaults(document):
    """Layer a stored document over the current defaults in place.

    Backward compatibility (issue #459 review, MAJOR-1): a stored document
    written by an *older* schema version (pre-migration, or a first
    interaction served by an old pod during a rolling deploy) is missing
    known keys outright. ``apply_patch`` and ``to_markdown`` must still
    serve it, so any absent known key is backfilled from
    :func:`default_preferences` before validation. Values the user actually
    set are never clobbered, and unknown (additive, newer-version) keys are
    left untouched — exactly the keys this schema version does not know are
    skipped here and preserved by the callers' deep copy.
    """
    def fill(defaults, node):
        if not isinstance(defaults, dict) or not isinstance(node, dict):
            return
        for key, default in defaults.items():
            if key not in node:
                node[key] = copy.deepcopy(default)
            elif isinstance(default, dict) and isinstance(node[key], dict):
                fill(default, node[key])

    fill(default_preferences(), document)


def _get(doc, path):
    parts = _split_path(path)
    node = doc
    for part in parts:
        node = node[part]
    return node


def _set(doc, path, value):
    parts = _split_path(path)
    node = doc
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def _get_optional(document, path):
    """Like :func:`_get`, but returns ``None`` for a path that is absent.

    Used by :func:`unsupported_criteria` so it tolerates a document that
    predates a given criterion key (pre-migration) instead of raising.
    """
    node = document
    for part in _split_path(path):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _criterion_is_set(spec, value, default):
    """Whether *value* represents the user actually expressing this criterion,
    as opposed to the field's un-set default (or a missing key outright, for
    a stored document that predates the key)."""
    if value is None:
        return False
    if spec in ("str_list", "float_map"):
        return bool(value)
    if spec == "str":
        # ``notes`` defaults to "" and enum leaves like ``basis``/``period``
        # default to a concrete value; both count as unset at their default.
        return bool(value) and value != default
    return value != default


def unsupported_criteria(document):
    """Return the ordered list of criterion keys the user has set that the
    matching engine cannot evaluate (registered ``UNSUPPORTED`` in
    :data:`CRITERION_SUPPORT`).

    A criterion is considered "set" when it differs from its schema default
    (a non-empty list/map, or a non-default scalar). Order follows
    :data:`CRITERION_SUPPORT`'s definition order, which is deterministic.
    """
    defaults = default_preferences()
    result = []
    for path, support in CRITERION_SUPPORT.items():
        if support != UNSUPPORTED:
            continue
        spec, _dynamic = _resolve_spec(path)
        value = _get_optional(document, path)
        if _criterion_is_set(spec, value, _get_optional(defaults, path)):
            result.append(path)
    return result


# ---------------------------------------------------------------------------
# Typed patch operations
# ---------------------------------------------------------------------------
def validate_patch(patch):
    """Validate patch structure. Pure: performs no mutation.

    A patch is ``{"set": {...}, "remove": {...}}`` (either side may be omitted,
    but at least one non-empty section is required). ``set`` values replace the
    target field; ``remove`` deletes list items or dictionary keys or resets a
    scalar/subtree to its default. Unknown fields and ambiguous operations
    raise.
    """
    if not isinstance(patch, dict):
        raise AmbiguousPatchError("Patch must be a JSON object")
    extra = set(patch) - {"set", "remove"}
    if extra:
        raise AmbiguousPatchError(
            "Unknown patch key(s): {}".format(", ".join(sorted(extra)))
        )
    set_part = patch.get("set")
    remove_part = patch.get("remove")
    if set_part is not None and not isinstance(set_part, dict):
        raise AmbiguousPatchError("'set' must be an object of path -> value")
    if remove_part is not None and not isinstance(remove_part, dict):
        raise AmbiguousPatchError("'remove' must be an object of path -> value")
    if not set_part and not remove_part:
        raise AmbiguousPatchError("Patch must contain a non-empty 'set' or 'remove'")

    if set_part:
        for path in set_part:
            spec, dynamic = _resolve_spec(path)
            value = set_part[path]
            if dynamic:
                raise AmbiguousPatchError(
                    f"Cannot 'set' a dynamic inner key directly: {path!r}"
                )
            if isinstance(spec, dict):
                # Replace a whole subtree: validate it as a full node.
                _validate_node_value(spec, value)
            else:
                validate_value(path, spec, value)

    if remove_part:
        for path in remove_part:
            spec, dynamic = _resolve_spec(path)
            value = remove_part[path]
            if spec == "str_list":
                if not isinstance(value, list):
                    raise AmbiguousPatchError(
                        f"'remove' for list field {path!r} must list items"
                    )
                for item in value:
                    _validate_str(item, path)
            elif spec == "float_map":
                if not isinstance(value, list):
                    raise AmbiguousPatchError(
                        f"'remove' for map field {path!r} must list keys"
                    )
                for key in value:
                    _validate_str(key, path)
            elif dynamic or isinstance(spec, dict) or spec in ("int", "float", "bool", "str"):
                # Remove a dict key or reset a scalar/subtree to default.
                if value is not None:
                    raise AmbiguousPatchError(
                        f"'remove' for {path!r} must use null to reset/delete"
                    )
            else:
                # Defensive: str_list/float_map/dynamic/scalar/dict specs are all
                # handled above; unreachable under the current schema.
                raise AmbiguousPatchError(
                    f"'remove' not supported for {path!r}"
                )  # pragma: no cover


def _validate_node_value(node_spec, value):
    """Validate a value as a full nested node (for whole-subtree 'set')."""
    if not isinstance(value, dict):
        raise InvalidValueError("Subtree value must be an object")
    # Validate by walking with the node spec as the root.
    def walk(spec, node, prefix):
        node_spec2 = spec
        if isinstance(node_spec2, dict):
            if not isinstance(node, dict):  # pragma: no cover - no dict-of-dict in schema
                raise InvalidValueError(f"{prefix!r} must be an object")
            missing = [k for k in node_spec2 if k not in node]
            unknown = [k for k in node if k not in node_spec2]
            if missing:
                raise InvalidValueError("Missing required field(s): {}".format(", ".join(missing)))
            if unknown:
                raise UnknownFieldError("Unknown field(s): {}".format(", ".join(unknown)))
            for key, sub in node_spec2.items():
                walk(sub, node[key], f"{prefix}.{key}" if prefix else key)
            return
        validate_value(prefix, node_spec2, node)
    walk(node_spec, value, "")


def _is_value_equal(spec, value, current):
    if spec == "float_map":
        return value == current
    if spec == "str_list":
        return value == current
    if spec in ("int", "float"):
        if value is None and current is None:
            return True
        if value is None or current is None:
            return False
        return float(value) == float(current)
    return value == current


def apply_patch(document, patch):
    """Apply a validated patch to a full document.

    Returns ``(new_document, change_count)``. Raises on invalid/ambiguous
    patches. Equivalent (no-op) patches produce ``change_count == 0`` and an
    unchanged deep copy, making repeats idempotent.
    """
    validate_patch(patch)
    new_doc = copy.deepcopy(document)
    # Backward compatibility (issue #459 review, MAJOR-1): a stored document
    # written by an older schema version is missing known keys outright
    # (pre-migration, or a first interaction served by an old pod during a
    # rolling deploy). Backfill only the absent keys from the current
    # defaults so the document remains patchable and renderable; values the
    # user set are never clobbered, and unknown additive keys are untouched.
    _fill_missing_defaults(new_doc)
    # Forward compatibility (epic #454): validate only the known portion of
    # the stored document. Additive fields written by a newer schema version
    # ride along in ``new_doc`` untouched: they are never rejected, modified,
    # or dropped by this path, and a patch cannot address them (strict patch
    # rule), so old pods never corrupt newer pods' fields.
    _validate_known_fields(new_doc)
    changes = 0

    set_part = patch.get("set") or {}
    remove_part = patch.get("remove") or {}

    # Resolve paths once so set/remove share validation of targets.
    for path, value in set_part.items():
        spec, dynamic = _resolve_spec(path)
        if isinstance(spec, dict):
            normalized = copy.deepcopy(value)
            _validate_node_value(spec, normalized)
        else:
            normalized = validate_value(path, spec, value)
        current = _get(new_doc, path)
        if isinstance(spec, dict):
            # Forward compatibility (issue #466 review): replacing a known
            # subtree must not drop additive nested keys a newer schema
            # version wrote inside it; they are preserved verbatim, exactly
            # like top-level unknown keys. The patch itself stays strict —
            # it may not *address* unknown keys — but a replace never
            # destroys them.
            normalized = _merge_unknown(normalized, _extract_unknown(current, spec))
        if not _is_value_equal(spec, normalized, current):
            _set(new_doc, path, normalized)
            changes += 1

    for path, value in remove_part.items():
        spec, dynamic = _resolve_spec(path)
        parts = _split_path(path)
        if spec == "str_list":
            target = new_doc
            for part in parts[:-1]:
                target = target[part]
            before = len(target[parts[-1]])
            target[parts[-1]] = [item for item in target[parts[-1]] if item not in set(value)]
            changes += before - len(target[parts[-1]])
        elif spec == "float_map":
            target = new_doc
            for part in parts[:-1]:  # pragma: no cover - float_map is depth-1 only
                target = target[part]
            for key in value:
                if key in target[parts[-1]]:
                    del target[parts[-1]][key]
                    changes += 1
        elif dynamic:  # priorities.<key>
            container = new_doc
            for part in parts[:-2]:  # pragma: no cover - dynamic keys are depth-2 only
                container = container[part]
            inner = container[parts[-2]]
            if parts[-1] in inner:
                del inner[parts[-1]]
                changes += 1
        else:
            # Reset an internal node subtree to its real defaults.
            fresh = _get(default_preferences(), path)
            current = _get(new_doc, path)
            if not _is_value_equal(spec, fresh, current):
                _set(new_doc, path, fresh)
                changes += 1

    return new_doc, changes


# ---------------------------------------------------------------------------
# Diff / propose / undo lifecycle (issue #466)
# ---------------------------------------------------------------------------
def _diff_base(document):
    """Deep-copy *document* with absent known keys backfilled from defaults.

    Mirrors the scratch normalization :func:`apply_patch` performs before
    applying a patch, so diff old-values match the document the patch ran
    against exactly.
    """
    base = copy.deepcopy(document)
    _fill_missing_defaults(base)
    return base


def _diff_anchor_path(path):
    """Return the set-able anchor path for a patch path.

    A dynamic float_map key (``priorities.<criterion>``) cannot be addressed
    by a ``set`` operation, so its diff (and inverse undo patch) anchors on
    the parent map path instead.
    """
    _spec, dynamic = _resolve_spec(path)
    if dynamic:
        return ".".join(_split_path(path)[:-1])
    return path


def _diff_changes(base, new_doc, patch):
    """Walk a validated patch's touched paths and return changed entries.

    Each entry is ``{"path", "old", "new"}`` for exactly the paths the patch
    altered, anchored at set-able paths (dynamic float_map keys collapse to
    their parent map). Paths whose value did not change are omitted.
    """
    changes = []
    seen = set()
    for section in ("set", "remove"):
        for path in (patch.get(section) or {}):
            anchor = _diff_anchor_path(path)
            if anchor in seen:
                continue
            seen.add(anchor)
            old = _get_optional(base, anchor)
            new = _get_optional(new_doc, anchor)
            if old != new:
                changes.append({
                    "path": anchor,
                    "old": copy.deepcopy(old),
                    "new": copy.deepcopy(new),
                })
    return changes


def diff_patch(document, patch):
    """Pure field-level diff of a patch against a document.

    Returns ``(changes, change_count)`` where ``changes`` is a list of
    ``{"path", "old", "new"}`` entries and ``change_count`` is exactly the
    second element of :func:`apply_patch` for the same inputs. Performs no
    I/O and mutates nothing; invalid patches raise the same errors
    :func:`apply_patch` raises.
    """
    base = _diff_base(document)
    new_doc, change_count = apply_patch(document, patch)
    return _diff_changes(base, new_doc, patch), change_count


def build_undo_token(expected_revision, prior_document):
    """Build an opaque undo token capturing the full pre-apply document.

    The token carries the stored document exactly as it was before the apply
    (byte-identical, including any pre-v3 missing keys and any additive keys
    written by a newer schema version) plus the post-apply revision the
    restore may be applied against. Capturing the whole prior document —
    rather than an inverse patch reconstructed from the diff — is what makes
    a byte-identical restore possible for every supported stored shape
    (issue #466 review): an inverse ``set``-only patch can never un-backfill
    keys :func:`apply_patch` layered onto an older document, nor restore
    unknown additive nested keys a subtree replace had to preserve. Returns
    ``None`` when nothing changed (there is nothing to undo).
    """
    if prior_document is None:
        return None
    return {
        "expected_revision": expected_revision,
        "document": copy.deepcopy(prior_document),
    }


def propose_patch_for_user(user, patch, *, scope="account"):
    """Read-only proposal of a patch: diff + base revision, never a write.

    Performs no database write of any kind: the stored row (when one exists),
    its ``modified``/``revision``, and the audit trail are all untouched, and
    no row is created for a user without one. Validation is identical to the
    apply path — the same :func:`validate_patch`/:func:`apply_patch`
    machinery runs here — so a patch rejected on apply is rejected here too.

    ``scope="search"`` additionally returns the in-memory
    ``effective_document`` the patch would produce (for a this-search-only
    filter); it is never persisted.
    """
    if scope not in ("account", "search"):
        raise AmbiguousPatchError(f"Unknown proposal scope: {scope!r}")
    pref = UserPreference.objects.filter(user=user).first()
    if pref is None:
        document = default_preferences()
        base_revision = 0
        base_modified = None
    else:
        document = pref.preferences
        base_revision = pref.revision
        base_modified = pref.modified
    base = _diff_base(document)
    new_doc, change_count = apply_patch(document, patch)
    result = {
        "base_revision": base_revision,
        "base_modified": base_modified,
        "changes": _diff_changes(base, new_doc, patch),
        "change_count": change_count,
        "scope": scope,
        "unsupported_criteria": unsupported_criteria(new_doc),
    }
    if scope == "search":
        result["effective_document"] = new_doc
    return result


def effective_document(user, patch):
    """Pure effective document for a this-search-only patch (never persisted)."""
    pref = UserPreference.objects.filter(user=user).first()
    document = pref.preferences if pref is not None else default_preferences()
    new_doc, _changes = apply_patch(document, patch)
    return new_doc


def _validate_stored_shape(document, spec=_FIELD_SPEC, prefix=""):
    """Validate the known keys present in a previously-stored document.

    Tolerant in both directions, exactly like the store's forward/backward
    compatibility stance: keys this schema version knows but the document
    lacks (a pre-v3 stored shape) are skipped, and additive keys written by a
    newer schema version are opaque and preserved verbatim. Every known key
    that IS present is strictly type-checked, so a tampered undo token cannot
    smuggle a corrupt value into a known field. Returns nothing; raises on a
    corrupt known value.
    """
    if not isinstance(document, dict):
        raise InvalidValueError("Preferences must be a JSON object")
    for key, sub_spec in spec.items():
        if key not in document:
            continue  # older stored shape: absent known keys are tolerated
        child = "{}.{}".format(prefix, key) if prefix else key
        if isinstance(sub_spec, dict):
            _validate_stored_shape(document[key], sub_spec, child)
        else:
            validate_value(child, sub_spec, document[key])


#: Ceiling on a client-held undo token's serialized size. A legitimate token
#: is one stored document (bounded by the field caps) plus a revision; this
#: leaves generous headroom while rejecting unbounded payloads.
_MAX_UNDO_DOCUMENT_BYTES = 64 * 1024


def undo_preference_change(user, undo_token):
    """Restore an undo token's captured document under its revision precondition.

    The token's captured pre-apply document is restored byte-identically —
    including any pre-v3 missing keys and additive newer-version keys — but
    only when the stored revision still equals the token's post-apply
    revision; an intervening edit (including a reset or a delete/recreate,
    both of which advance the revision lineage) raises
    :class:`StalePreferenceError` (with ``current_revision``) and writes
    nothing, so an old undo can never overwrite a later edit. A ``None`` or
    malformed token — including one whose ``expected_revision`` is missing,
    null, a boolean, or negative — raises :class:`AmbiguousPatchError`.

    The restored document is owner-scoped and shape-checked
    (:func:`_validate_stored_shape`): known fields present in the document
    are type-checked, absent known keys (older shapes) and unknown additive
    keys (newer shapes) ride along untouched, so the restore is byte-identical
    across every supported stored shape.
    """
    if not isinstance(undo_token, dict):
        raise AmbiguousPatchError("Invalid undo token")
    expected_revision = undo_token.get("expected_revision")
    if (
        expected_revision is None
        or isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise AmbiguousPatchError("Invalid undo token")
    document = undo_token.get("document")
    if not isinstance(document, dict):
        raise AmbiguousPatchError("Invalid undo token")
    try:
        import json as _json

        if len(_json.dumps(document).encode("utf-8")) > _MAX_UNDO_DOCUMENT_BYTES:
            raise AmbiguousPatchError("Invalid undo token")
    except (TypeError, ValueError):
        raise AmbiguousPatchError("Invalid undo token")
    _validate_stored_shape(document)
    with transaction.atomic():
        pref = _lock(user)
        if pref is None:
            raise StalePreferenceError(
                "preference row was deleted while the undo was in flight",
                current_revision=0,
            )
        _check_stale_revision(pref, expected_revision)
        restored = copy.deepcopy(document)
        change_count = _count_differences(pref.preferences, restored)
        change_id = None
        if change_count:
            pref.preferences = restored
            pref.preferences_markdown = to_markdown(restored)
            pref.revision = pref.revision + 1
            pref.save(update_fields=[
                "preferences", "preferences_markdown", "revision", "modified",
            ])
            _audit(user, UserPreferenceAudit.Action.PATCHED, change_count)
            change_id = "{}:{}".format(user.pk, pref.revision)
            _schedule_recompute(change_id)
        result = _serialize(pref)
        result["changed"] = bool(change_count)
        result["revision"] = pref.revision
        result["changes"] = []
        result["undo"] = None
        result["change_id"] = change_id
        return result


def _count_differences(a, b):
    """Count leaf-level differences between two documents (audit metadata only)."""
    if a == b:
        return 0
    if isinstance(a, dict) and isinstance(b, dict):
        return sum(_count_differences(a.get(k), b.get(k)) for k in set(a) | set(b))
    return 1


# ---------------------------------------------------------------------------
# Recompute hook (issue #466; the durable consumer belongs to #475)
# ---------------------------------------------------------------------------
def _resolve_recompute_hook():
    """Resolve the settings-named recompute hook, or ``None`` when unset."""
    path = (getattr(settings, "PREFERENCE_RECOMPUTE_HOOK", "") or "").strip()
    if not path:
        return None
    return import_string(path)


def _schedule_recompute(change_id):
    """Schedule exactly one logical recomputation for a committed change.

    Fires from ``transaction.on_commit`` — never on a rolled-back apply —
    with ``change_id = f"{user_id}:{post_apply_revision}"``. Hook failures
    are logged and swallowed: preference saves must never depend on the
    recompute consumer's availability (the durable mechanism is #475).
    """
    def _fire():
        try:
            hook = _resolve_recompute_hook()
            if hook is not None:
                hook(change_id)
        except Exception:  # noqa: BLE001 - best-effort boundary, never raise
            logger.exception("preference recompute hook failed")

    transaction.on_commit(_fire)


# ---------------------------------------------------------------------------
# Deterministic markdown projection
# ---------------------------------------------------------------------------
def _escape_md(value):
    """Escape markdown/control characters so values are safe in prompts."""
    text = str(value).replace("\x00", "")
    escapees = {"\\": "\\\\", "`": "\\`", "*": "\\*", "_": "\\_", "#": "\\#",
                "[": "\\[", "]": "\\]", "<": "&lt;", ">": "&gt;"}
    for char, rep in escapees.items():
        text = text.replace(char, rep)
    # Collapse newlines/tabs inside list/scalar values into single spaces.
    text = " ".join(text.split())
    return text


def _money(value, currency):
    if value is None:
        return "Not specified"
    # Currency is user-supplied; escape markdown control characters before
    # embedding (n1).
    return f"{_escape_md(currency).upper()} {int(value):,}"


def to_markdown(document):
    """Render a validated document to deterministic, escaped markdown.

    Tolerates additive fields this schema version does not know (they are
    preserved in the stored JSON but never projected into markdown), so a
    document written by a newer schema version renders during a rolling
    deploy. Missing known keys (a stored document from an *older* schema
    version) are backfilled from defaults on a scratch copy first, so the
    same document also renders before its migration has run (issue #459
    review, MAJOR-1).
    """
    document = copy.deepcopy(document)
    _fill_missing_defaults(document)
    _validate_known_fields(document)
    lines = ["# Career Preferences", ""]
    comp = document["compensation"]
    lines.append("## Compensation")
    lines.append("- Minimum salary: {}".format(_money(comp["minimum_salary"], comp["currency"])))
    if comp["equity_minimum_percent"] is not None:
        lines.append("- Minimum equity target: {:.1f}%".format(comp["equity_minimum_percent"]))
    else:
        lines.append("- Minimum equity target: Not specified")
    if comp["require_public_company"] is not None:
        lines.append("- Require public company: {}".format("Yes" if comp["require_public_company"] else "No"))
    if comp["basis"] != "base":
        lines.append("- Compensation basis: {}".format(comp["basis"].capitalize()))
    if comp["period"] != "year":
        lines.append("- Compensation period: {}".format(comp["period"].capitalize()))
    if comp["minimum_total_compensation"] is not None:
        lines.append("- Minimum total compensation: {}".format(
            _money(comp["minimum_total_compensation"], comp["currency"])
        ))
    if comp["equity_liquidity_required"] is not None:
        lines.append("- Equity liquidity required: {}".format(
            "Yes" if comp["equity_liquidity_required"] else "No"
        ))
    if comp["acceptable_liquidity_events"]:
        lines.append("- Acceptable liquidity events: {}".format(
            ", ".join(_escape_md(e) for e in comp["acceptable_liquidity_events"])
        ))
    lines.append("")

    roles = document["roles"]
    if roles["families"] or roles["titles"] or roles["seniority"]:
        lines.append("## Roles")
        if roles["families"]:
            lines.append("- Families: {}".format(", ".join(_escape_md(f) for f in roles["families"])))
        if roles["titles"]:
            lines.append("- Titles: {}".format(", ".join(_escape_md(t) for t in roles["titles"])))
        if roles["seniority"]:
            lines.append("- Seniority: {}".format(", ".join(_escape_md(s) for s in roles["seniority"])))
        lines.append("")

    _section(lines, "Culture", document["culture"])
    wl = document["work_location"]
    lines.append("## Work Location")
    lines.append("- Modes: {}".format(", ".join(_escape_md(m) for m in wl["modes"]) or "Any"))
    lines.append("- Countries: {}".format(", ".join(_escape_md(c) for c in wl["countries"]) or "Any"))
    lines.append("- Require onsite: {}".format("Yes" if wl["require_onsite"] else "No"))
    if wl["max_in_office_days"] is not None:
        lines.append("- Max in-office days/week: {}".format(wl["max_in_office_days"]))
    if wl["office_days_exact"] is not None:
        lines.append("- Exact in-office days/week: {}".format(wl["office_days_exact"]))
    lines.append("")
    geo = document["geography"]
    lines.append("## Geography")
    lines.append("- Regions: {}".format(", ".join(_escape_md(r) for r in geo["regions"]) or "Any"))
    lines.append("- Remote friendly: {}".format("Yes" if geo["remote_friendly"] else "No"))
    lines.append("")

    _section(lines, "Industry", document["industry"])
    _section(lines, "Funding Stage", document["funding_stage"])
    vest = document["vesting"]
    lines.append("## Vesting")
    lines.append("- Max cliff (months): {}".format(vest["max_cliff_months"] if vest["max_cliff_months"] is not None else "Not specified"))
    lines.append("- Max vesting (months): {}".format(vest["max_vesting_months"] if vest["max_vesting_months"] is not None else "Not specified"))
    lines.append("- Prefer accelerated vesting: {}".format("Yes" if vest["prefer_accelerated"] else "No"))
    lines.append("")
    exc = document["exclusions"]
    lines.append("## Exclusions")
    lines.append("- Companies: {}".format(", ".join(_escape_md(c) for c in exc["companies"]) or "None"))
    lines.append("- Titles: {}".format(", ".join(_escape_md(t) for t in exc["titles"]) or "None"))
    lines.append("- Industries: {}".format(", ".join(_escape_md(i) for i in exc["industries"]) or "None"))
    lines.append("- Locations: {}".format(", ".join(_escape_md(l) for l in exc["locations"]) or "None"))
    lines.append("")
    if document["priorities"]:
        lines.append("## Priorities")
        for key in sorted(document["priorities"]):
            lines.append("- {}: {:.2f}".format(_escape_md(key), document["priorities"][key]))
        lines.append("")
    notes = document["notes"]
    if notes:
        lines.append("## Notes")
        lines.append(_escape_md(notes))
        lines.append("")
    _unsupported_section(lines, document)
    return "\n".join(lines).rstrip() + "\n"


def _section(lines, heading, items):
    lines.append(f"## {heading}")
    if items:
        for item in items:
            lines.append(f"- {_escape_md(item)}")
    else:
        lines.append("- None")
    lines.append("")


def _unsupported_section(lines, document):
    """Append the "Unsupported requirements" section, if any criterion the
    user set is not yet evaluated by the matching engine.

    Never rendered in a way that implies satisfaction: each entry is listed
    with an explicit "Not evaluated by matching yet." caveat, and the whole
    section is omitted when nothing unsupported is set.
    """
    unsupported = unsupported_criteria(document)
    if not unsupported:
        return
    lines.append("## Unsupported requirements")
    for key in unsupported:
        lines.append("- {}: Not evaluated by matching yet.".format(_escape_md(key)))
    lines.append("")


# ---------------------------------------------------------------------------
# Owner-scoped application services
# ---------------------------------------------------------------------------
def _audit(user, action, change_count=0):
    UserPreferenceAudit.objects.create(
        user=user,
        action=action,
        schema_version=SCHEMA_VERSION,
        change_count=change_count,
    )


def _fetch_or_create(user):
    """Fetch the user's preference row, creating it on first interaction.

    No-row behavior is documented: reads/patches/resets implicitly create the
    row with valid defaults on first agent interaction. Creation is
    transaction-safe: creation is wrapped in ``transaction.atomic()`` and a
    concurrent first-interaction create is reconciled via the unique
    ``OneToOneField`` (M2) instead of raising an unhandled ``IntegrityError``.
    """
    with transaction.atomic():
        return _create_or_fetch(user)


def _create_or_fetch(user):
    """Return the row, creating it if absent, racing safely (M3).

    Must be called inside an active transaction. If a concurrent request
    created the row between our check and create, the ``IntegrityError`` from
    the unique ``user`` constraint is swallowed and the winner's row is
    returned.
    """
    doc = default_preferences()
    try:
        # Inner atomic() = savepoint so an IntegrityError from a concurrent
        # create rolls back cleanly and the recovery get() below can still run
        # in the caller's surrounding transaction (M3).
        with transaction.atomic():
            pref = UserPreference.objects.create(
                user=user,
                preferences=doc,
                preferences_markdown=to_markdown(doc),
                schema_version=SCHEMA_VERSION,
                revision=_recreate_revision_floor(user),
            )
        created = True
    except IntegrityError:
        # Lost the race to a concurrent first-interaction create.
        pref = UserPreference.objects.get(user=user)
        created = False
    if created:
        _audit(user, UserPreferenceAudit.Action.CREATED)
    return pref


def _fetch_or_create_pending(user):
    """Race-safe create (inside the caller's transaction) from a lock miss."""
    return _create_or_fetch(user)


def _recreate_revision_floor(user):
    """Return the starting revision for a newly created preference row.

    ``0`` for a genuine first interaction. After a delete, the delete's audit
    row recorded ``revision + 1`` as the floor (issue #466 review), so a
    re-created row continues the monotonic lineage instead of restarting at
    0 — an undo token captured before the delete can never match the new row.
    """
    floor = (
        UserPreferenceAudit.objects.filter(
            user=user, action=UserPreferenceAudit.Action.DELETED
        )
        .order_by("-change_count", "-id")
        .values_list("change_count", flat=True)
        .first()
    )
    return floor or 0


def _lock(user):
    """Return the row under an active transaction with a row lock, or None."""
    return (
        UserPreference.objects.select_for_update().filter(user=user).first()
    )


def _validate_expected_revision(expected_revision):
    """Validate a revision precondition value.

    ``None`` is the explicit low-level opt-out (legacy timestamp path) and is
    returned unchanged. Every other value must be a real non-negative
    integer: booleans, strings, floats, and negatives are rejected so a
    tampered or malformed precondition can never silently downgrade to the
    unchecked legacy path (issue #466 review).
    """
    if expected_revision is None:
        return None
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise InvalidValueError(
            "expected_revision must be a non-negative integer"
        )
    if expected_revision < 0:
        raise InvalidValueError(
            "expected_revision must be a non-negative integer"
        )
    return expected_revision


def _check_stale_revision(pref, expected_revision):
    """Enforce the revision precondition (issue #466).

    Raises :class:`StalePreferenceError` carrying the machine-readable
    ``current_revision`` when the stored revision no longer matches.
    """
    if expected_revision is None:
        return
    if pref.revision != expected_revision:
        raise StalePreferenceError(
            "Preference revision changed; expected {}, current {}".format(
                expected_revision, pref.revision
            ),
            current_revision=pref.revision,
        )


def _check_stale(pref, expected_modified):
    if expected_modified is PREFERENCE_ABSENT:
        # No row existed at turn start, but one exists now: it was created
        # (or re-created) mid-turn, so the patch must not overwrite it.
        raise StalePreferenceError(
            "preference row was created while the turn was in flight"
        )
    if expected_modified is None:
        return
    expected = _normalize_ts(expected_modified)
    if expected is None:
        raise StalePreferenceError("Expected an ISO-8601 modified timestamp")
    if pref.modified != expected:
        raise StalePreferenceError(
            f"Preference version changed; expected {expected.isoformat()}, current {pref.modified.isoformat()}"
        )


def _normalize_ts(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            from django.utils.dateparse import parse_datetime
            parsed = parse_datetime(value)
        except (ValueError, TypeError):  # pragma: no cover - parse_datetime returns None, never raises
            parsed = None
        if parsed is None:
            return None
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, _dt_tz.utc)
        return parsed
    if hasattr(value, "tzinfo"):
        return value
    return None


def read(user):
    """Owner-scoped read. Model default, full document + markdown returned."""
    pref = _fetch_or_create(user)
    return _serialize(pref)


def export(user):
    """Owner-scoped full export (document + markdown + metadata)."""
    pref = _fetch_or_create(user)
    _audit(user, UserPreferenceAudit.Action.EXPORTED)
    return {
        "schema_version": pref.schema_version,
        "modified": pref.modified,
        "preferences": copy.deepcopy(pref.preferences),
        "markdown": pref.preferences_markdown,
    }


def apply_patch_to_user(user, patch, expected_modified=None, *, expected_revision=None):
    """Validate and transactionally apply a typed patch for the user.

    Returns a dict: ``{preferences, markdown, modified, changed, revision,
    changes, undo, change_id}``. Raises ``StalePreferenceError`` if the
    optimistic precondition no longer matches (the change is not applied).
    Regenerates markdown after every accepted patch.

    ``expected_revision`` (issue #466) is the revision precondition: the patch
    applies only when the stored ``revision`` still equals it, and a mismatch
    raises :class:`StalePreferenceError` carrying the machine-readable
    ``current_revision``. ``expected_modified`` is the turn-start baseline and
    may be:

    * the row's ``modified`` datetime — the row must still carry it at commit;
    * :data:`PREFERENCE_ABSENT` — no row existed at turn start; the patch is
      applied only if the row is *still* absent (a mid-turn creation, e.g. a
      concurrent first-interaction, makes the patch stale);
    * ``None`` — explicit opt-out of the check (legacy low-level callers).

    When ``expected_revision`` is supplied it wins and ``expected_modified``
    is ignored; when it is not supplied, ``expected_modified`` behaves
    exactly as before (including the sentinel and the ``None`` opt-out).

    If the row existed at turn start but is gone at apply time (the user
    deleted their preferences mid-turn), the patch fails closed with
    ``StalePreferenceError`` instead of silently re-creating the row.

    A committed apply that changes at least one field advances ``revision``
    by exactly one, returns the field-level ``changes`` and a ``set``-only
    inverse ``undo`` token, and schedules exactly one logical recomputation
    via ``transaction.on_commit`` with
    ``change_id = f"{user_id}:{post_apply_revision}"``. A no-op apply
    (``change_count == 0``) leaves ``revision``, ``modified``, and the audit
    trail untouched, returns ``undo = None``, and schedules nothing.

    ``expected_revision`` is validated before any lock is taken: a non-``None``
    value that is not a non-negative integer (booleans, null tokens, strings,
    negatives) raises :class:`InvalidValueError` instead of silently falling
    back to the unchecked legacy path (issue #466 review).
    """
    _validate_expected_revision(expected_revision)
    with transaction.atomic():
        pref = _lock(user)
        if pref is None:
            if expected_revision is not None:
                if expected_revision == 0:
                    # Revision 0 = "no committed revision yet": the row is
                    # still absent, so creating it satisfies the precondition.
                    pref = _fetch_or_create_pending(user)
                else:
                    raise StalePreferenceError(
                        "preference row was deleted while the turn was in flight",
                        current_revision=0,
                    )
            elif expected_modified is PREFERENCE_ABSENT or expected_modified is None:
                # Still absent since the turn-start capture: safe to create.
                pref = _fetch_or_create_pending(user)
            else:
                # The row existed at turn start but was deleted mid-turn;
                # re-creating it would resurrect the deleted preference state.
                raise StalePreferenceError(
                    "preference row was deleted while the turn was in flight"
                )
        else:
            if expected_revision is not None:
                _check_stale_revision(pref, expected_revision)
            else:
                _check_stale(pref, expected_modified)
        new_doc, changes = apply_patch(pref.preferences, patch)
        change_entries = []
        undo_token = None
        change_id = None
        if changes:
            change_entries = _diff_changes(_diff_base(pref.preferences), new_doc, patch)
            new_revision = pref.revision + 1
            # The undo token captures the full pre-apply stored document so
            # the restore is byte-identical for every supported shape
            # (issue #466 review), not an inverse patch rebuilt from the diff.
            undo_token = build_undo_token(new_revision, pref.preferences)
            pref.preferences = new_doc
            pref.preferences_markdown = to_markdown(new_doc)
            pref.revision = new_revision
            pref.save(update_fields=[
                "preferences", "preferences_markdown", "revision", "modified",
            ])
            _audit(user, UserPreferenceAudit.Action.PATCHED, changes)
            change_id = "{}:{}".format(user.pk, new_revision)
            _schedule_recompute(change_id)
        result = _serialize(pref)
        result["changed"] = bool(changes)
        result["revision"] = pref.revision
        result["changes"] = change_entries
        result["undo"] = undo_token
        result["change_id"] = change_id
        return result


def reset(user, expected_modified=None):
    """Owner-scoped reset to valid empty defaults.

    ``changed`` is True when the stored document actually differed from the
    defaults (so a repeat reset on already-default preferences is idempotent).
    """
    with transaction.atomic():
        pref = _lock(user)
        if pref is None:
            pref = _fetch_or_create_pending(user)
            # Newly created default row: nothing to change.
            fresh = default_preferences()
        else:
            _check_stale(pref, expected_modified)
            fresh = default_preferences()
            # Forward compatibility: a newer schema version may have written
            # additive fields this version cannot interpret. Resetting the
            # known fields must never drop them; they are reattached to the
            # fresh defaults verbatim and stay owned by the newer version.
            _merge_unknown(fresh, _extract_unknown(pref.preferences))
        changed = not (
            pref.preferences == fresh
            and pref.preferences_markdown == to_markdown(fresh)
        )
        if changed:
            pref.preferences = fresh
            pref.preferences_markdown = to_markdown(fresh)
            # A reset is a committed canonical document change, so it advances
            # the monotonic revision exactly like a patch (issue #466 review):
            # an undo token captured before the reset no longer matches and
            # can never overwrite the reset state.
            pref.revision = pref.revision + 1
            pref.save(update_fields=[
                "preferences", "preferences_markdown", "revision", "modified",
            ])
            _audit(user, UserPreferenceAudit.Action.RESET)
            _schedule_recompute("{}:{}".format(user.pk, pref.revision))
        result = _serialize(pref)
        result["changed"] = bool(changed)
        result["revision"] = pref.revision
        return result


def delete_user_preference(user, expected_modified=None):
    """Owner-scoped delete of the preference row.

    Returns ``{"deleted": bool, "existed": bool}``. Deleting a non-existent
    preference is a documented no-op (no row is created).

    ``expected_modified`` (optional) enforces optimistic concurrency: the
    delete is rejected with :class:`StalePreferenceError` if the row changed
    since the caller read it (m2). The row is locked with
    ``select_for_update`` to serialize with concurrent patches (m3).
    """
    with transaction.atomic():
        pref = (
            UserPreference.objects.select_for_update().filter(user=user).first()
        )
        if pref is None:
            return {"deleted": False, "existed": False}
        _check_stale(pref, expected_modified)
        # Record the revision floor a future re-create must start above
        # (issue #466 review): deleting the row must not restart the
        # monotonic revision lineage at 0, or an undo token captured before
        # the delete could match a re-created row that climbed back to the
        # same revision and overwrite it. The floor survives the row itself
        # because audit rows belong to the user, not the preference row.
        floor = pref.revision + 1
        pref.delete()
        _audit(user, UserPreferenceAudit.Action.DELETED, floor)
        return {"deleted": True, "existed": True}


def _serialize(pref):
    return {
        "schema_version": pref.schema_version,
        "modified": pref.modified,
        "preferences": copy.deepcopy(pref.preferences),
        "markdown": pref.preferences_markdown,
    }
