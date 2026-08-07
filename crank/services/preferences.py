# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Validated preference lifecycle services.

This module owns the version-1 preference schema, typed patch semantics,
deterministic markdown projection, and owner-scoped application services
(create-on-first-interaction, read, export, reset, delete, patch).

Design rules from the issue:

* Unknown fields and ambiguous operations fail validation.
* Markdown is derived output and is never accepted as canonical state.
* Concurrent updates must not silently overwrite a newer preference version,
  enforced with a row lock (``select_for_update``) plus an optimistic
  ``modified`` timestamp check that also works on backends where FOR UPDATE is
  a no-op (e.g. SQLite).
* Preference contents are never written to logs; audit rows store metadata only.
"""
import copy

from django.db import transaction
from django.utils import timezone

from crank.models.preference import (
    SCHEMA_VERSION,
    UserPreference,
    UserPreferenceAudit,
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
    """The preference changed since the caller's last read."""


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
    },
    "culture": "str_list",  # preferred culture attributes
    "work_location": {
        "modes": "str_list",          # e.g. onsite, hybrid, remote
        "countries": "str_list",
        "require_onsite": "bool",
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
}


def default_preferences():
    """Return a fresh, schema-valid empty preferences document."""
    return {
        "compensation": {
            "minimum_salary": None,
            "currency": "USD",
            "equity_minimum_percent": None,
        },
        "culture": [],
        "work_location": {"modes": [], "countries": [], "require_onsite": None},
        "geography": {"regions": [], "remote_friendly": None},
        "industry": [],
        "funding_stage": [],
        "vesting": {
            "max_cliff_months": None,
            "max_vesting_months": None,
            "prefer_accelerated": None,
        },
        "exclusions": {"companies": [], "titles": [], "industries": [], "locations": []},
        "priorities": {},
        "notes": "",
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
        raise UnknownFieldError("Unknown preference field: {!r}".format(path))
    if isinstance(node, dict) and node:
        return node, False  # internal node -> subtree to set/reset
    if node in _LEAF_TYPES:
        return node, False
    raise UnknownFieldError("Unknown preference field: {!r}".format(path))


def _validate_str(value, field, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise InvalidValueError(
            "{!r} must be a non-empty string".format(field)
        )
    if len(value) > MAX_SCALAR_LENGTH:
        raise InvalidValueError(
            "Field {!r} exceeds maximum length of {}".format(field, MAX_SCALAR_LENGTH)
        )
    return value


def validate_value(field, leaf_type, value):
    """Validate a single value against a leaf type; return the normalized value."""
    if leaf_type == "bool":
        if value is None:
            return None
        if not isinstance(value, bool):
            raise InvalidValueError(
                "Field {!r} must be a boolean".format(field)
            )
        return value
    if leaf_type in ("int", "float"):
        if value is None:
            return value
        if isinstance(value, bool):
            raise InvalidValueError("Field {!r} must not be a boolean".format(field))
        if leaf_type == "int":
            if not isinstance(value, int):
                raise InvalidValueError("Field {!r} must be an integer".format(field))
            if value < 0:
                raise InvalidValueError("Field {!r} must be non-negative".format(field))
            return value
        if not isinstance(value, (int, float)):
            raise InvalidValueError(
                "Field {!r} must be a number".format(field)
            )
        return float(value)
    if leaf_type == "str":
        # "notes" is free-form and may legitimately be empty.
        return _validate_str(value, field, allow_empty=(field == "notes"))
    if leaf_type == "str_list":
        if not isinstance(value, list):
            raise InvalidValueError("Field {!r} must be a list".format(field))
        if len(value) > MAX_LIST_LENGTH:
            raise InvalidValueError(
                "Field {!r} exceeds {} items".format(field, MAX_LIST_LENGTH)
            )
        out = []
        for item in value:
            out.append(_validate_str(item, field))
        return out
    if leaf_type == "float_map":
        if not isinstance(value, dict):
            raise InvalidValueError("Field {!r} must be an object".format(field))
        if len(value) > MAX_PRIORITIES:
            raise InvalidValueError(
                "Field {!r} exceeds {} keys".format(field, MAX_PRIORITIES)
            )
        out = {}
        for key, weight in value.items():
            _validate_str(key, field)
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise InvalidValueError(
                    "Priority {!r} must be a number".format(key)
                )
            weight = float(weight)
            if weight < MIN_PRIORITY or weight > MAX_PRIORITY:
                raise InvalidValueError(
                    "Priority {!r} must be within {}-{}".format(
                        key, MIN_PRIORITY, MAX_PRIORITY
                    )
                )
            out[key] = weight
        return out
    raise InvalidValueError("Unknown leaf type {!r}".format(leaf_type))


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
    _validate_notes(document.get("notes", ""))

def _validate_notes(notes):
    if notes is not None:
        if not isinstance(notes, str):
            raise InvalidValueError("notes must be a string")
        if len(notes) > MAX_NOTES_LENGTH:
            raise InvalidValueError(
                "notes exceeds maximum length of {}".format(MAX_NOTES_LENGTH)
            )


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
                    "Cannot 'set' a dynamic inner key directly: {!r}".format(path)
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
                        "'remove' for list field {!r} must list items".format(path)
                    )
                for item in value:
                    _validate_str(item, path)
            elif spec == "float_map":
                if not isinstance(value, list):
                    raise AmbiguousPatchError(
                        "'remove' for map field {!r} must list keys".format(path)
                    )
                for key in value:
                    _validate_str(key, path)
            elif dynamic or isinstance(spec, dict) or spec in ("int", "float", "bool", "str"):
                # Remove a dict key or reset a scalar/subtree to default.
                if value is not None:
                    raise AmbiguousPatchError(
                        "'remove' for {!r} must use null to reset/delete".format(path)
                    )
            else:
                raise AmbiguousPatchError(
                    "'remove' not supported for {!r}".format(path)
                )


def _validate_node_value(node_spec, value):
    """Validate a value as a full nested node (for whole-subtree 'set')."""
    if not isinstance(value, dict):
        raise InvalidValueError("Subtree value must be an object")
    # Validate by walking with the node spec as the root.
    def walk(spec, node, prefix):
        node_spec2 = spec
        if isinstance(node_spec2, dict):
            if not isinstance(node, dict):
                raise InvalidValueError("{!r} must be an object".format(prefix))
            missing = [k for k in node_spec2 if k not in node]
            unknown = [k for k in node if k not in node_spec2]
            if missing:
                raise InvalidValueError("Missing required field(s): {}".format(", ".join(missing)))
            if unknown:
                raise UnknownFieldError("Unknown field(s): {}".format(", ".join(unknown)))
            for key, sub in node_spec2.items():
                walk(sub, node[key], ".".join([prefix, key]) if prefix else key)
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
    validate_document(new_doc)
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
            for part in parts[:-1]:
                target = target[part]
            for key in value:
                if key in target[parts[-1]]:
                    del target[parts[-1]][key]
                    changes += 1
        elif dynamic:  # priorities.<key>
            container = new_doc
            for part in parts[:-2]:
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
    return "{} {:,}".format(currency.upper(), int(value))


def to_markdown(document):
    """Render a validated document to deterministic, escaped markdown."""
    validate_document(document)
    lines = ["# Career Preferences", ""]
    comp = document["compensation"]
    lines.append("## Compensation")
    lines.append("- Minimum salary: {}".format(_money(comp["minimum_salary"], comp["currency"])))
    if comp["equity_minimum_percent"] is not None:
        lines.append("- Minimum equity target: {:.1f}%".format(comp["equity_minimum_percent"]))
    else:
        lines.append("- Minimum equity target: Not specified")
    lines.append("")

    _section(lines, "Culture", document["culture"])
    wl = document["work_location"]
    lines.append("## Work Location")
    lines.append("- Modes: {}".format(", ".join(_escape_md(m) for m in wl["modes"]) or "Any"))
    lines.append("- Countries: {}".format(", ".join(_escape_md(c) for c in wl["countries"]) or "Any"))
    lines.append("- Require onsite: {}".format("Yes" if wl["require_onsite"] else "No"))
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
    return "\n".join(lines).rstrip() + "\n"


def _section(lines, heading, items):
    lines.append("## {}".format(heading))
    if items:
        for item in items:
            lines.append("- {}".format(_escape_md(item)))
    else:
        lines.append("- None")
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
    row with valid defaults on first agent interaction.
    """
    try:
        return UserPreference.objects.get(user=user)
    except UserPreference.DoesNotExist:
        doc = default_preferences()
        pref = UserPreference.objects.create(
            user=user,
            preferences=doc,
            preferences_markdown=to_markdown(doc),
            schema_version=SCHEMA_VERSION,
        )
        _audit(user, UserPreferenceAudit.Action.CREATED)
        return pref


def _lock(user):
    """Return the row under an active transaction with a row lock, or None."""
    return (
        UserPreference.objects.select_for_update().filter(user=user).first()
    )


def _check_stale(pref, expected_modified):
    if expected_modified is None:
        return
    expected = _normalize_ts(expected_modified)
    if expected is None:
        raise StalePreferenceError("Expected an ISO-8601 modified timestamp")
    if pref.modified != expected:
        raise StalePreferenceError(
            "Preference version changed; expected {}, current {}".format(
                expected.isoformat(), pref.modified.isoformat()
            )
        )


def _normalize_ts(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            from django.utils.dateparse import parse_datetime
            parsed = parse_datetime(value)
        except (ValueError, TypeError):
            parsed = None
        if parsed is None:
            return None
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.utc)
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


def apply_patch_to_user(user, patch, expected_modified=None):
    """Validate and transactionally apply a typed patch for the user.

    Returns a dict: ``{preferences, markdown, modified, changed}``. Raises
    ``StalePreferenceError`` if ``expected_modified`` no longer matches (the
    change is not applied). Regenerates markdown after every accepted patch.
    """
    with transaction.atomic():
        pref = _lock(user)
        if pref is None:
            pref = _fetch_or_create_pending(user)
        else:
            _check_stale(pref, expected_modified)
        new_doc, changes = apply_patch(pref.preferences, patch)
        if changes:
            pref.preferences = new_doc
            pref.preferences_markdown = to_markdown(new_doc)
            pref.save(update_fields=["preferences", "preferences_markdown", "modified"])
            _audit(user, UserPreferenceAudit.Action.PATCHED, changes)
        result = _serialize(pref)
        result["changed"] = bool(changes)
        return result


def _fetch_or_create_pending(user):
    """Create (inside the caller's transaction) from a lock miss."""
    doc = default_preferences()
    pref = UserPreference.objects.create(
        user=user,
        preferences=doc,
        preferences_markdown=to_markdown(doc),
        schema_version=SCHEMA_VERSION,
    )
    _audit(user, UserPreferenceAudit.Action.CREATED)
    return pref


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
        changed = not (
            pref.preferences == fresh
            and pref.preferences_markdown == to_markdown(fresh)
        )
        if changed:
            pref.preferences = fresh
            pref.preferences_markdown = to_markdown(fresh)
            pref.save(update_fields=["preferences", "preferences_markdown", "modified"])
            _audit(user, UserPreferenceAudit.Action.RESET)
        result = _serialize(pref)
        result["changed"] = bool(changed)
        return result


def reset_defaults(user, expected_modified=None):
    """Deprecated alias kept for backward clarity; use :func:`reset`."""
    return reset(user, expected_modified=expected_modified)


def delete_user_preference(user):
    """Owner-scoped delete of the preference row.

    Returns ``{"deleted": bool, "existed": bool}``. Deleting a non-existent
    preference is a documented no-op (no row is created).
    """
    with transaction.atomic():
        pref = UserPreference.objects.filter(user=user).first()
        if pref is None:
            return {"deleted": False, "existed": False}
        pref.delete()
        _audit(user, UserPreferenceAudit.Action.DELETED)
        return {"deleted": True, "existed": True}


def _serialize(pref):
    return {
        "schema_version": pref.schema_version,
        "modified": pref.modified,
        "preferences": copy.deepcopy(pref.preferences),
        "markdown": pref.preferences_markdown,
    }