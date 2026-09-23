# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import copy

import pytest
from django.contrib.auth import get_user_model

from crank.models.preference import SCHEMA_VERSION, UserPreference, UserPreferenceAudit
from crank.services import preferences as prefs
from crank.services.preferences import (
    AmbiguousPatchError,
    InvalidValueError,
    StalePreferenceError,
    UnknownFieldError,
)


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(
        username="alice",
        password="pw",
        email="alice@example.com",
    )


@pytest.fixture
def other_user(db):
    return get_user_model().objects.create_user(
        username="bob",
        password="pw",
        email="bob@example.com",
    )


# ---------------------------------------------------------------------------
# Pure schema / patch behavior (no DB)
# ---------------------------------------------------------------------------
class TestSchema:
    def test_default_is_schema_valid(self):
        doc = prefs.default_preferences()
        prefs.validate_document(doc)  # must not raise
        assert doc["compensation"]["currency"] == "USD"
        assert doc["notes"] == ""

    def test_unknown_field_fails(self):
        doc = prefs.default_preferences()
        doc["bogus"] = []
        with pytest.raises(UnknownFieldError):
            prefs.validate_document(doc)

    def test_missing_required_field_fails(self):
        doc = prefs.default_preferences()
        del doc["culture"]
        with pytest.raises(InvalidValueError):
            prefs.validate_document(doc)

    def test_unknown_nested_field_fails(self):
        doc = prefs.default_preferences()
        doc["compensation"]["signing_bonus"] = 5
        with pytest.raises(UnknownFieldError):
            prefs.validate_document(doc)

    @pytest.mark.parametrize(
        "path,value",
        [
            ("culture", "not-a-list"),
            ("culture", ["ok" * 1000]),
            ("compensation.minimum_salary", 3.5),   # int leaf
            ("compensation.minimum_salary", True),  # bool rejected for int
            ("compensation.equity_minimum_percent", "high"),
            ("work_location.require_onsite", "yes"),
            ("geography.remote_friendly", 1),
            ("vesting.max_cliff_months", -5),
            ("notes", 42),
            ("notes", "x" * 5000),
            ("priorities", {"culture": 2.0}),
            ("work_location", {}),      # dict-spec subtree -> _validate_node_value missing-keys
            ("priorities", {"culture": "high"}),
            ("compensation.basis", "weekly"),
            ("compensation.period", "fortnight"),
            ("compensation.currency", "US"),        # 2 letters
            ("compensation.currency", "USDX"),      # 4 letters
            ("compensation.currency", "U5D"),       # non-letter
            ("work_location.office_days_exact", -1),
            ("work_location.office_days_exact", 8),
            ("importance", {"compensation.minimum_salary": -0.1}),
            ("importance", {"compensation.minimum_salary": 1.1}),
            ("importance", {"roles.unknown_criterion": 0.5}),  # unregistered key
        ],
    )
    def test_invalid_values_fail(self, path, value):
        doc = prefs.default_preferences()
        _set(doc, path, value)
        # Validate the standalone value directly for clarity.
        spec = prefs._resolve_spec(path)[0]
        if isinstance(spec, dict):
            with pytest.raises(InvalidValueError):
                prefs._validate_node_value(spec, value)
        else:
            with pytest.raises(InvalidValueError):
                prefs.validate_value(path, spec, value)


class TestV3Schema:
    """Issue #459: explicit roles, compensation basis/period/liquidity,
    exact office days, importance, and scope."""

    def test_default_is_v3_superset_of_v2(self):
        doc = prefs.default_preferences()
        prefs.validate_document(doc)
        # Every v2 key retains its v2 default.
        assert doc["compensation"]["minimum_salary"] is None
        assert doc["compensation"]["currency"] == "USD"
        assert doc["compensation"]["equity_minimum_percent"] is None
        assert doc["compensation"]["require_public_company"] is None
        assert doc["culture"] == []
        assert doc["work_location"]["modes"] == []
        assert doc["work_location"]["countries"] == []
        assert doc["work_location"]["require_onsite"] is None
        assert doc["work_location"]["max_in_office_days"] is None
        assert doc["geography"] == {"regions": [], "remote_friendly": None}
        assert doc["industry"] == []
        assert doc["funding_stage"] == []
        assert doc["vesting"] == {
            "max_cliff_months": None,
            "max_vesting_months": None,
            "prefer_accelerated": None,
        }
        assert doc["exclusions"] == {
            "companies": [], "titles": [], "industries": [], "locations": [],
        }
        assert doc["priorities"] == {}
        assert doc["notes"] == ""
        # New v3 keys are present with additive defaults.
        assert doc["roles"] == {"families": [], "titles": [], "seniority": []}
        assert doc["compensation"]["basis"] == "base"
        assert doc["compensation"]["period"] == "year"
        assert doc["compensation"]["minimum_total_compensation"] is None
        assert doc["compensation"]["equity_liquidity_required"] is None
        assert doc["compensation"]["acceptable_liquidity_events"] == []
        assert doc["work_location"]["office_days_exact"] is None
        assert doc["importance"] == {}
        assert doc["scope"] == {"countries": [], "role_families": []}

    def test_office_days_exact_boundaries_accepted(self):
        assert prefs.validate_value("work_location.office_days_exact", "int", 0) == 0
        assert prefs.validate_value("work_location.office_days_exact", "int", 7) == 7

    def test_importance_boundaries_accepted(self):
        result = prefs.validate_value(
            "importance", "float_map",
            {"compensation.minimum_salary": 0.0, "culture": 1.0},
        )
        assert result == {"compensation.minimum_salary": 0.0, "culture": 1.0}

    def test_currency_lowercase_normalizes_to_uppercase(self):
        assert prefs.validate_value("compensation.currency", "str", "usd") == "USD"
        assert prefs.validate_value("compensation.currency", "str", "eur") == "EUR"

    def test_basis_and_period_accept_valid_enum_values(self):
        assert prefs.validate_value("compensation.basis", "str", "base") == "base"
        assert prefs.validate_value("compensation.basis", "str", "total") == "total"
        assert prefs.validate_value("compensation.period", "str", "year") == "year"
        assert prefs.validate_value("compensation.period", "str", "month") == "month"
        assert prefs.validate_value("compensation.period", "str", "hour") == "hour"


class TestUnsupportedCriteria:
    def test_empty_document_has_no_unsupported_criteria(self):
        assert prefs.unsupported_criteria(prefs.default_preferences()) == []

    def test_document_with_only_supported_values_has_no_unsupported_criteria(self):
        doc = prefs.default_preferences()
        doc["compensation"]["minimum_salary"] = 150000
        doc["culture"] = ["transparent"]
        assert prefs.unsupported_criteria(doc) == []

    def test_minimum_total_compensation_set_is_reported(self):
        doc = prefs.default_preferences()
        doc["compensation"]["minimum_total_compensation"] = 250000
        assert "compensation.minimum_total_compensation" in prefs.unsupported_criteria(doc)

    def test_basis_total_is_reported(self):
        """Issue #459 review, MAJOR-3: the matching engine never reads
        ``compensation.basis``, so ``basis == \"total\"`` must be surfaced
        as unsupported rather than silently applying ``minimum_salary`` as
        a base-salary filter (the exact defect #459 names)."""
        doc = prefs.default_preferences()
        doc["compensation"]["basis"] = "total"
        doc["compensation"]["minimum_salary"] = 250000
        unsupported = prefs.unsupported_criteria(doc)
        assert "compensation.basis" in unsupported

    def test_period_non_default_is_reported(self):
        """The engine never reads ``compensation.period`` either."""
        doc = prefs.default_preferences()
        doc["compensation"]["period"] = "hour"
        assert "compensation.period" in prefs.unsupported_criteria(doc)

    def test_require_onsite_set_is_reported(self):
        """The engine never reads ``work_location.require_onsite``."""
        doc = prefs.default_preferences()
        doc["work_location"]["require_onsite"] = True
        assert "work_location.require_onsite" in prefs.unsupported_criteria(doc)

    def test_importance_set_is_reported(self):
        """Issue #459 review, MAJOR-2: a non-empty importance map is a
        hard-requirement signal the matching engine cannot evaluate, so it
        must be surfaced, not silently counted as satisfied."""
        doc = prefs.default_preferences()
        doc["importance"]["compensation.minimum_salary"] = 1.0
        assert "importance" in prefs.unsupported_criteria(doc)

    def test_notes_set_is_reported_but_empty_notes_is_not(self):
        """``notes`` is UNSUPPORTED (nothing in matching reads it), and its
        empty-string default must not count as set (issue #459 review,
        MINOR-3)."""
        doc = prefs.default_preferences()
        assert "notes" not in prefs.unsupported_criteria(doc)
        doc["notes"] = "prefers public transit"
        assert "notes" in prefs.unsupported_criteria(doc)

    def test_priorities_are_supported(self):
        """``priorities`` is genuinely read by ``project_criteria`` — it is
        one of the better-supported criteria (issue #459 review, MINOR-3)."""
        assert prefs.CRITERION_SUPPORT["priorities"] == prefs.SUPPORTED
        doc = prefs.default_preferences()
        doc["priorities"]["culture"] = 0.8
        assert "priorities" not in prefs.unsupported_criteria(doc)

    def test_importance_accepts_priorities_key(self):
        """The registry doubles as the allow-list for importance keys, so
        registering ``priorities`` makes it a valid importance target."""
        result = prefs.validate_value("importance", "float_map", {"priorities": 1.0})
        assert result == {"priorities": 1.0}

    def test_pre_migration_document_missing_v3_keys_has_no_unsupported_criteria(self):
        """A stored v2 document (pre-0035) lacks every v3 key outright, not
        just at its default value. ``unsupported_criteria`` must tolerate the
        absent path (via ``_get_optional``) rather than raising, and treat a
        missing key the same as an unset default."""
        doc = prefs.default_preferences()
        del doc["roles"]
        del doc["scope"]
        for key in (
            "basis", "period", "minimum_total_compensation",
            "equity_liquidity_required", "acceptable_liquidity_events",
        ):
            del doc["compensation"][key]
        del doc["work_location"]["office_days_exact"]
        assert prefs.unsupported_criteria(doc) == []

    def test_basis_total_with_total_set_is_reported(self):
        doc = prefs.default_preferences()
        doc["compensation"]["basis"] = "total"
        doc["compensation"]["minimum_total_compensation"] = 250000
        unsupported = prefs.unsupported_criteria(doc)
        assert "compensation.minimum_total_compensation" in unsupported
        assert "compensation.basis" in unsupported

    def test_ordering_is_deterministic(self):
        doc = prefs.default_preferences()
        doc["scope"]["countries"] = ["US"]
        doc["roles"]["families"] = ["engineering"]
        doc["work_location"]["office_days_exact"] = 2
        first = prefs.unsupported_criteria(doc)
        second = prefs.unsupported_criteria(doc)
        assert first == second
        assert first == [key for key in prefs.CRITERION_SUPPORT if key in first]

    def test_all_unsupported_criteria_registered_unsupported(self):
        doc = prefs.default_preferences()
        doc["roles"]["families"] = ["eng"]
        doc["roles"]["titles"] = ["Staff Engineer"]
        doc["roles"]["seniority"] = ["staff"]
        doc["compensation"]["minimum_total_compensation"] = 1
        doc["compensation"]["equity_liquidity_required"] = True
        doc["compensation"]["acceptable_liquidity_events"] = ["acquisition"]
        doc["work_location"]["office_days_exact"] = 3
        doc["scope"]["countries"] = ["US"]
        doc["scope"]["role_families"] = ["engineering"]
        unsupported = prefs.unsupported_criteria(doc)
        for key in unsupported:
            assert prefs.CRITERION_SUPPORT[key] == prefs.UNSUPPORTED
        assert len(unsupported) == 9

    def test_registry_covers_every_canonical_leaf(self):
        """AC-5 / MINOR-3: every canonical criterion key in ``_FIELD_SPEC``
        is registered — no leaf is silently outside the support registry."""
        def leaves(spec, prefix=""):
            for key, sub in spec.items():
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(sub, dict):
                    yield from leaves(sub, path)
                else:
                    yield path

        missing = [path for path in leaves(prefs._FIELD_SPEC) if path not in prefs.CRITERION_SUPPORT]
        assert missing == []


class TestPatch:
    def test_set_replaces_list(self):
        doc = prefs.default_preferences()
        new, changes = prefs.apply_patch(doc, {"set": {"culture": ["transparent", "people-centric"]}})
        assert changes == 1
        assert new["culture"] == ["transparent", "people-centric"]

    def test_set_scalar_and_nested(self):
        doc = prefs.default_preferences()
        new, changes = prefs.apply_patch(
            doc,
            {"set": {"compensation.minimum_salary": 150000, "work_location.modes": ["hybrid"]}},
        )
        assert changes == 2
        assert new["compensation"]["minimum_salary"] == 150000
        assert new["work_location"]["modes"] == ["hybrid"]

    def test_set_whole_subtree(self):
        doc = prefs.default_preferences()
        new, changes = prefs.apply_patch(
            doc,
            {"set": {"compensation": {
                "minimum_salary": 200000,
                "currency": "USD",
                "equity_minimum_percent": 0.5,
                "require_public_company": None,
                "basis": "base",
                "period": "year",
                "minimum_total_compensation": None,
                "equity_liquidity_required": None,
                "acceptable_liquidity_events": [],
            }}},
        )
        assert changes == 1
        assert new["compensation"]["minimum_salary"] == 200000

    def test_set_whole_priorities_map(self):
        # Individual priority keys are set explicitly as a full map (typed set).
        doc = prefs.default_preferences()
        new, changes = prefs.apply_patch(doc, {"set": {"priorities": {"industry": 0.5}}})
        assert changes == 1
        assert new["priorities"]["industry"] == 0.5

    def test_remove_list_items(self):
        doc = prefs.default_preferences()
        doc = prefs.apply_patch(
            doc, {"set": {"exclusions.companies": ["Acme", "Globex", "Initech"]}}
        )[0]
        new, changes = prefs.apply_patch(doc, {"remove": {"exclusions.companies": ["Acme", "Initech"]}})
        assert changes == 2
        assert new["exclusions"]["companies"] == ["Globex"]

    def test_remove_priorities_key(self):
        doc = prefs.default_preferences()
        doc = prefs.apply_patch(doc, {"set": {"priorities": {"culture": 0.9, "industry": 0.5}}})[0]
        new, changes = prefs.apply_patch(doc, {"remove": {"priorities.industry": None}})
        assert changes == 1
        assert new["priorities"] == {"culture": 0.9}

    def test_remove_whole_map_by_keys(self):
        doc = prefs.default_preferences()
        doc = prefs.apply_patch(doc, {"set": {"priorities": {"culture": 0.9}}})[0]
        new, changes = prefs.apply_patch(doc, {"remove": {"priorities": ["culture"]}})
        assert changes == 1
        assert new["priorities"] == {}

    def test_remove_scalar_resets_to_default(self):
        doc = prefs.default_preferences()
        doc = prefs.apply_patch(doc, {"set": {"geography.remote_friendly": True}})[0]
        assert doc["geography"]["remote_friendly"] is True
        new, changes = prefs.apply_patch(doc, {"remove": {"geography.remote_friendly": None}})
        assert changes == 1
        assert new["geography"]["remote_friendly"] is None

    def test_remove_subtree_resets_to_defaults(self):
        doc = prefs.default_preferences()
        doc = prefs.apply_patch(doc, {"set": {"compensation.minimum_salary": 300000}})[0]
        new, changes = prefs.apply_patch(doc, {"remove": {"compensation": None}})
        assert changes == 1
        assert new["compensation"] == prefs.default_preferences()["compensation"]

    def test_unknown_field_in_patch_fails(self):
        doc = prefs.default_preferences()
        with pytest.raises(UnknownFieldError):
            prefs.apply_patch(doc, {"set": {"nonexistent": 1}})

    def test_ambiguous_remove_for_list(self):
        doc = prefs.default_preferences()
        with pytest.raises(AmbiguousPatchError):
            prefs.apply_patch(doc, {"remove": {"culture": None}})

    def test_ambiguous_set_for_dynamic_priority(self):
        doc = prefs.default_preferences()
        with pytest.raises(AmbiguousPatchError):
            prefs.apply_patch(doc, {"set": {"priorities.culture": 0.5}})

    def test_empty_patch_fails(self):
        doc = prefs.default_preferences()
        with pytest.raises(AmbiguousPatchError):
            prefs.apply_patch(doc, {})

    def test_idempotent_repeat_patch(self):
        doc = prefs.default_preferences()
        first, c1 = prefs.apply_patch(doc, {"set": {"culture": ["transparent"]}})
        assert c1 == 1
        repeat, c2 = prefs.apply_patch(first, {"set": {"culture": ["transparent"]}})
        assert c2 == 0
        assert repeat["culture"] == ["transparent"]
        # Removing an absent item is also a no-op.
        _noop, c3 = prefs.apply_patch(first, {"remove": {"culture": ["missing"]}})
        assert c3 == 0

    def test_apply_patch_does_not_mutate_input(self):
        doc = prefs.default_preferences()
        original = copy.deepcopy(doc)
        new, _ = prefs.apply_patch(doc, {"set": {"culture": ["x"]}})
        assert doc == original
        assert new != original


class TestV3Patch:
    """apply_patch coverage for every new v3 path (issue #459)."""

    def test_set_roles_families(self):
        doc = prefs.default_preferences()
        new, changes = prefs.apply_patch(doc, {"set": {"roles.families": ["engineering"]}})
        assert changes == 1
        assert new["roles"]["families"] == ["engineering"]

    def test_set_equal_value_is_a_noop(self):
        doc = prefs.default_preferences()
        first, c1 = prefs.apply_patch(doc, {"set": {"roles.families": ["engineering"]}})
        assert c1 == 1
        repeat, c2 = prefs.apply_patch(first, {"set": {"roles.families": ["engineering"]}})
        assert c2 == 0
        assert repeat["roles"]["families"] == ["engineering"]

    def test_remove_scalar_resets_basis_to_default(self):
        doc, _ = prefs.apply_patch(
            prefs.default_preferences(), {"set": {"compensation.basis": "total"}}
        )
        new, changes = prefs.apply_patch(doc, {"remove": {"compensation.basis": None}})
        assert changes == 1
        assert new["compensation"]["basis"] == "base"

    def test_remove_list_item_from_roles_titles(self):
        doc, _ = prefs.apply_patch(
            prefs.default_preferences(),
            {"set": {"roles.titles": ["Staff Engineer", "Principal Engineer"]}},
        )
        new, changes = prefs.apply_patch(doc, {"remove": {"roles.titles": ["Staff Engineer"]}})
        assert changes == 1
        assert new["roles"]["titles"] == ["Principal Engineer"]

    def test_remove_importance_key_via_dynamic_float_map_path(self):
        doc, _ = prefs.apply_patch(
            prefs.default_preferences(),
            {"set": {"importance": {"compensation.minimum_salary": 1.0}}},
        )
        new, changes = prefs.apply_patch(
            doc, {"remove": {"importance": ["compensation.minimum_salary"]}}
        )
        assert changes == 1
        assert new["importance"] == {}

    def test_patch_targeting_unregistered_roles_field_fails(self):
        doc = prefs.default_preferences()
        with pytest.raises(UnknownFieldError):
            prefs.apply_patch(doc, {"set": {"roles.unknown": ["x"]}})

    def test_patch_targeting_unregistered_scope_field_fails(self):
        """``scope`` is a fixed two-field object (countries, role_families);
        any other key is rejected the same way any unknown field is —
        strict-patch (UnknownFieldError) — since scope has no dynamic keys
        to validate against the criterion registry."""
        doc = prefs.default_preferences()
        with pytest.raises(UnknownFieldError):
            prefs.apply_patch(doc, {"set": {"scope.unknown": ["x"]}})

    def test_set_compensation_liquidity_fields(self):
        doc = prefs.default_preferences()
        new, changes = prefs.apply_patch(
            doc,
            {"set": {
                "compensation.equity_liquidity_required": True,
                "compensation.acceptable_liquidity_events": ["acquisition", "ipo"],
            }},
        )
        assert changes == 2
        assert new["compensation"]["equity_liquidity_required"] is True
        assert new["compensation"]["acceptable_liquidity_events"] == ["acquisition", "ipo"]

    def test_set_work_location_office_days_exact(self):
        doc = prefs.default_preferences()
        new, changes = prefs.apply_patch(doc, {"set": {"work_location.office_days_exact": 2}})
        assert changes == 1
        assert new["work_location"]["office_days_exact"] == 2


def _v2_shaped_document():
    """A stored pre-0035 (schema v2) document: the v3 default minus the
    v3-only keys, as an old row looks before migration 0035 runs."""
    doc = prefs.default_preferences()
    del doc["roles"]
    del doc["importance"]
    del doc["scope"]
    for key in (
        "basis", "period", "minimum_total_compensation",
        "equity_liquidity_required", "acceptable_liquidity_events",
    ):
        del doc["compensation"][key]
    del doc["work_location"]["office_days_exact"]
    return doc


class TestPreMigrationDocumentServing:
    """Issue #459 review, MAJOR-1: stored v1/v2 documents must still patch
    and render markdown (AC-9). ``apply_patch`` and ``to_markdown`` backfill
    missing known keys from the current defaults without clobbering set
    values or touching unknown additive keys."""

    def test_apply_patch_on_v2_document_backfills_missing_v3_keys(self):
        v2_doc = _v2_shaped_document()
        v2_doc["compensation"]["minimum_salary"] = 175000
        new, changes = prefs.apply_patch(v2_doc, {"set": {"notes": "hello"}})
        assert changes == 1
        assert new["notes"] == "hello"
        # Set values survive; missing keys land at their v3 defaults.
        assert new["compensation"]["minimum_salary"] == 175000
        assert new["compensation"]["basis"] == "base"
        assert new["compensation"]["period"] == "year"
        assert new["roles"] == {"families": [], "titles": [], "seniority": []}
        assert new["importance"] == {}
        assert new["scope"] == {"countries": [], "role_families": []}
        assert new["work_location"]["office_days_exact"] is None

    def test_apply_patch_on_v2_document_does_not_mutate_input(self):
        v2_doc = _v2_shaped_document()
        original = copy.deepcopy(v2_doc)
        prefs.apply_patch(v2_doc, {"set": {"notes": "hello"}})
        assert v2_doc == original

    def test_apply_patch_on_v2_document_preserves_unknown_additive_keys(self):
        v2_doc = _v2_shaped_document()
        v2_doc["notifications"] = {"channel": "email"}
        new, _ = prefs.apply_patch(v2_doc, {"set": {"notes": "hello"}})
        assert new["notifications"] == {"channel": "email"}
        assert new["roles"] == {"families": [], "titles": [], "seniority": []}

    def test_apply_patch_can_set_v3_fields_on_v2_document(self):
        """Once backfilled, a patch may target the new v3 paths directly."""
        v2_doc = _v2_shaped_document()
        new, changes = prefs.apply_patch(v2_doc, {"set": {"roles.families": ["engineering"]}})
        assert changes == 1
        assert new["roles"]["families"] == ["engineering"]

    def test_to_markdown_on_v2_document_renders_without_raising(self):
        v2_doc = _v2_shaped_document()
        v2_doc["compensation"]["minimum_salary"] = 175000
        md = prefs.to_markdown(v2_doc)
        assert md.startswith("# Career Preferences\n")
        assert "USD 175,000" in md
        # Default-filled v3 keys render exactly as an untouched v3 document:
        # no v3-only lines appear.
        assert "## Roles" not in md
        assert "## Unsupported requirements" not in md
        assert "Compensation basis" not in md

    def test_to_markdown_on_v2_document_does_not_mutate_input(self):
        v2_doc = _v2_shaped_document()
        original = copy.deepcopy(v2_doc)
        prefs.to_markdown(v2_doc)
        assert v2_doc == original

    def test_to_markdown_on_v1_document_renders_without_raising(self):
        """The backfill covers every prior schema era: a v1 document is also
        missing the 0024 keys (require_public_company, max_in_office_days)."""
        v1_doc = _v2_shaped_document()
        del v1_doc["compensation"]["require_public_company"]
        del v1_doc["work_location"]["max_in_office_days"]
        md = prefs.to_markdown(v1_doc)
        assert md.startswith("# Career Preferences\n")
        new, changes = prefs.apply_patch(v1_doc, {"set": {"notes": "v1"}})
        assert changes == 1
        assert new["compensation"]["require_public_company"] is None
        assert new["work_location"]["max_in_office_days"] is None

    def test_fill_missing_defaults_never_clobbers_set_values(self):
        doc = _v2_shaped_document()
        doc["compensation"]["currency"] = "EUR"
        doc["culture"] = ["transparent"]
        prefs._fill_missing_defaults(doc)
        assert doc["compensation"]["currency"] == "EUR"
        assert doc["culture"] == ["transparent"]
        assert doc["compensation"]["basis"] == "base"


class TestMarkdown:
    def test_deterministic_output(self):
        doc = prefs.default_preferences()
        doc = prefs.apply_patch(doc, {"set": {"culture": ["transparent"], "notes": "d"}})[0]
        assert prefs.to_markdown(doc) == prefs.to_markdown(doc)

    def test_escapes_markdown_control_chars(self):
        doc = prefs.default_preferences()
        doc = prefs.apply_patch(
            doc,
            {"set": {"culture": ["*bold* _it_ `code` [x]"], "notes": "a # b < c > d"}},
        )[0]
        md = prefs.to_markdown(doc)
        assert "\\*bold\\*" in md
        assert "\\_it\\_" in md
        assert "\\`code\\`" in md
        assert "\\[x\\]" in md
        assert "&lt; c &gt;" in md
        assert "\\# b" in md
        # Raw unescaped control sequences must not survive.
        assert "*bold* _it_" not in md

    def test_header_and_sections(self):
        md = prefs.to_markdown(prefs.default_preferences())
        assert md.startswith("# Career Preferences\n")
        assert "## Compensation" in md
        assert "## Exclusions" in md

    def test_default_document_has_no_v3_sections(self):
        """With no v3 values set, no Roles/Unsupported requirements section
        appears and no new Compensation/Work Location lines are added: the
        markdown for an untouched document matches today's shape exactly."""
        md = prefs.to_markdown(prefs.default_preferences())
        assert "## Roles" not in md
        assert "## Unsupported requirements" not in md
        assert "Compensation basis" not in md
        assert "Compensation period" not in md
        assert "Minimum total compensation" not in md
        assert "Equity liquidity required" not in md
        assert "Acceptable liquidity events" not in md
        assert "Exact in-office days" not in md

    def test_unsupported_requirements_section_appears_when_set(self):
        doc = prefs.default_preferences()
        doc["compensation"]["minimum_total_compensation"] = 250000
        md = prefs.to_markdown(doc)
        assert "## Unsupported requirements" in md
        assert "compensation.minimum\\_total\\_compensation: Not evaluated by matching yet." in md

    def test_roles_and_liquidity_values_are_escaped(self):
        doc = prefs.default_preferences()
        doc["roles"]["families"] = ["*eng*"]
        doc["roles"]["titles"] = ["_staff_"]
        doc["roles"]["seniority"] = ["`senior`"]
        doc["compensation"]["acceptable_liquidity_events"] = ["secondary <sale>"]
        md = prefs.to_markdown(doc)
        assert "\\*eng\\*" in md
        assert "\\_staff\\_" in md
        assert "\\`senior\\`" in md
        assert "secondary &lt;sale&gt;" in md
        # Raw unescaped sequences must not survive.
        assert "*eng*" not in md
        assert "_staff_" not in md

    def test_roles_section_only_lists_set_lists(self):
        doc = prefs.default_preferences()
        doc["roles"]["families"] = ["engineering"]
        md = prefs.to_markdown(doc)
        roles_section = md.split("## Roles")[1].split("## Culture")[0]
        assert "- Families: engineering" in roles_section
        assert "- Titles:" not in roles_section
        assert "- Seniority:" not in roles_section

    def test_compensation_basis_and_period_only_render_when_non_default(self):
        doc = prefs.default_preferences()
        doc["compensation"]["basis"] = "total"
        doc["compensation"]["period"] = "hour"
        md = prefs.to_markdown(doc)
        assert "- Compensation basis: Total" in md
        assert "- Compensation period: Hour" in md

    def test_office_days_exact_renders_when_set(self):
        doc = prefs.default_preferences()
        doc["work_location"]["office_days_exact"] = 2
        md = prefs.to_markdown(doc)
        assert "- Exact in-office days/week: 2" in md


# ---------------------------------------------------------------------------
# Service layer (DB backed)
# ---------------------------------------------------------------------------
def _set(doc, path, value):
    parts = path.split(".")
    node = doc
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


class TestReadCreate:
    def test_first_interaction_creates_valid_row(self, user):
        pref = prefs.read(user)
        assert pref["schema_version"] == SCHEMA_VERSION
        assert pref["preferences"] == prefs.default_preferences()
        assert UserPreference.objects.filter(user=user).count() == 1
        assert UserPreferenceAudit.objects.filter(user=user, action="created").exists()

    def test_single_row_per_user(self, user):
        prefs.read(user)
        prefs.read(user)
        assert UserPreference.objects.filter(user=user).count() == 1


class TestPatchService:
    def test_patch_persists_and_regenerates_markdown(self, user):
        result = prefs.apply_patch_to_user(
            user, {"set": {"compensation.minimum_salary": 180000}}
        )
        assert result["changed"] is True
        assert result["preferences"]["compensation"]["minimum_salary"] == 180000
        assert "180,000" in result["markdown"]
        row = UserPreference.objects.get(user=user)
        assert row.preferences_markdown == result["markdown"]
        assert UserPreferenceAudit.objects.filter(user=user, action="patched").exists()

    def test_unknown_patch_makes_no_db_changes(self, user):
        initial = prefs.read(user)
        with pytest.raises(UnknownFieldError):
            prefs.apply_patch_to_user(user, {"set": {"nope": 1}})
        row = UserPreference.objects.get(user=user)
        assert row.preferences == initial["preferences"]
        assert row.preferences_markdown == initial["markdown"]

    def test_partial_invalid_patch_rolls_back_all_changes(self, user):
        initial = prefs.read(user)
        # One valid field plus one unknown field -> nothing is applied.
        with pytest.raises(UnknownFieldError):
            prefs.apply_patch_to_user(
                user,
                {"set": {"culture": ["transparent"], "bogus": "x"}},
            )
        row = UserPreference.objects.get(user=user)
        assert row.preferences == initial["preferences"]

    def test_invalid_value_rolls_back(self, user):
        # A failed patch on a fresh user seeds no row at all (rollback of create).
        with pytest.raises(InvalidValueError):
            prefs.apply_patch_to_user(user, {"set": {"culture": "nope"}})
        assert not UserPreference.objects.filter(user=user).exists()
        # A failed patch on an existing user leaves its data untouched.
        prefs.apply_patch_to_user(user, {"set": {"culture": ["a"]}})
        with pytest.raises(InvalidValueError):
            prefs.apply_patch_to_user(user, {"set": {"notes": 123}})
        row = UserPreference.objects.get(user=user)
        assert row.preferences["culture"] == ["a"]
        assert row.preferences["notes"] == ""

    def test_stale_expected_modified_rejected(self, user):
        first = prefs.apply_patch_to_user(user, {"set": {"culture": ["a"]}})
        expected = first["modified"]
        prefs.apply_patch_to_user(user, {"set": {"culture": ["a", "b"]}})
        with pytest.raises(StalePreferenceError):
            prefs.apply_patch_to_user(user, {"set": {"notes": "stale"}}, expected_modified=expected)
        row = UserPreference.objects.get(user=user)
        assert row.preferences["notes"] == ""

    def test_second_patch_after_read_works(self, user):
        first = prefs.apply_patch_to_user(user, {"set": {"culture": ["a"]}})
        second = prefs.apply_patch_to_user(
            user, {"set": {"culture": ["a", "b"]}}, expected_modified=first["modified"]
        )
        assert second["changed"] is True

    def test_repeated_equivalent_patch_idempotent(self, user):
        first = prefs.apply_patch_to_user(user, {"set": {"culture": ["a"]}})
        second = prefs.apply_patch_to_user(user, {"set": {"culture": ["a"]}})
        assert second["changed"] is False
        row = UserPreference.objects.get(user=user)
        assert row.modified == first["modified"]


class TestOwnership:
    def test_users_are_isolated(self, user, other_user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "mine"}})
        other = prefs.read(other_user)
        assert other["preferences"]["notes"] == ""

    def test_delete_only_affects_owner(self, user, other_user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "mine"}})
        prefs.read(other_user)
        result = prefs.delete_user_preference(user)
        assert result["deleted"] is True
        assert UserPreference.objects.filter(user=user).count() == 0
        assert UserPreference.objects.filter(user=other_user).count() == 1
        assert UserPreferenceAudit.objects.filter(user=user, action="deleted").exists()


class TestResetDeleteExport:
    def test_reset_restores_defaults(self, user):
        prefs.apply_patch_to_user(user, {"set": {"culture": ["x"], "notes": "hi"}})
        result = prefs.reset(user)
        assert result["preferences"] == prefs.default_preferences()
        assert result["changed"] is True
        assert UserPreferenceAudit.objects.filter(user=user, action="reset").exists()

    def test_reset_on_defaults_is_idempotent(self, user):
        result = prefs.reset(user)
        assert result["changed"] is False

    def test_reset_stale_rejected(self, user):
        first = prefs.apply_patch_to_user(user, {"set": {"culture": ["x"]}})
        prefs.apply_patch_to_user(user, {"set": {"culture": ["x", "y"]}})
        with pytest.raises(StalePreferenceError):
            prefs.reset(user, expected_modified=first["modified"])

    def test_export_returns_full_document_and_audits(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "export me"}})
        exported = prefs.export(user)
        assert exported["preferences"]["notes"] == "export me"
        assert exported["markdown"].startswith("# Career Preferences")
        assert exported["schema_version"] == SCHEMA_VERSION
        assert exported["modified"] is not None
        assert UserPreferenceAudit.objects.filter(user=user, action="exported").exists()

    def test_delete_non_existent_is_noop(self, user):
        result = prefs.delete_user_preference(user)
        assert result == {"deleted": False, "existed": False}
        assert UserPreference.objects.filter(user=user).count() == 0

    def test_delete_then_read_creates_fresh_row(self, user):
        prefs.delete_user_preference(user)
        fresh = prefs.read(user)
        assert fresh["preferences"] == prefs.default_preferences()

    def test_delete_cascades_nothing_to_user_data(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        assert UserPreferenceAudit.objects.filter(user=user).count() >= 1
        prefs.delete_user_preference(user)
        # Audit rows survive the preference deletion but never carry contents.
        assert UserPreferenceAudit.objects.filter(user=user).exists()


class TestAuditNoContents:
    def test_audit_rows_store_no_preference_values(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "supersecret", "culture": ["private"]}})
        for audit in UserPreferenceAudit.objects.filter(user=user):
            assert "supersecret" not in str(audit)
            assert "private" not in str(audit)
            assert audit.change_count >= 0

    def test_model_str_does_not_expose_contents(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "hidden-value"}})
        row = UserPreference.objects.get(user=user)
        assert "hidden-value" not in str(row)

class TestReviewFixes:
    def test_notes_over_100_chars_allowed(self, user):
        """M1: notes are capped at MAX_NOTES_LENGTH (2000), not the 100-char scalar cap."""
        long_notes = "n" * 500
        result = prefs.apply_patch_to_user(user, {"set": {"notes": long_notes}})
        assert result["preferences"]["notes"] == long_notes

    def test_notes_over_2000_chars_rejected(self, user):
        with pytest.raises(InvalidValueError):
            prefs.apply_patch_to_user(user, {"set": {"notes": "n" * 2001}})

    def test_currency_escaped_in_markdown(self, user):
        """compensation.currency is now a strict 3-ASCII-letter enum (issue
        #459 AC-4), so a value with markdown control characters can no
        longer reach the stored document through the validated write path;
        _money()'s _escape_md call is pinned directly instead."""
        with pytest.raises(prefs.InvalidValueError):
            prefs.apply_patch_to_user(
                user,
                {"set": {"compensation.currency": "US`D", "compensation.minimum_salary": 100000}},
            )
        assert prefs._money(100000, "US`D") == "US\\`D 100,000"

    def test_double_read_creates_single_row(self, user):
        """M2/M3: re-reading an existing row never duplicates it or double-audits create."""
        prefs.read(user)
        prefs.read(user)
        assert UserPreference.objects.filter(user=user).count() == 1
        assert (
            UserPreferenceAudit.objects.filter(
                user=user, action=UserPreferenceAudit.Action.CREATED
            ).count()
            == 1
        )

    def test_delete_stale_expected_modified_rejected(self, user):
        read = prefs.read(user)
        modified = read["modified"]
        prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        with pytest.raises(StalePreferenceError):
            prefs.delete_user_preference(user, expected_modified=modified)
        # The delete was rejected; the row survives.
        assert UserPreference.objects.filter(user=user).exists()

    def test_delete_no_stale_arg_is_noop(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        result = prefs.delete_user_preference(user)
        assert result == {"deleted": True, "existed": True}
        assert not UserPreference.objects.filter(user=user).exists()


class TestCoverageEdges:
    """Edge branches to meet the 99.25% Codecov patch target."""

    def test_split_path_rejects_empty_and_non_str(self):
        with pytest.raises(prefs.UnknownFieldError):
            prefs._resolve_spec("")
        with pytest.raises(prefs.UnknownFieldError):
            prefs._resolve_spec(5)

    def test_validate_value_unknown_leaf(self):
        with pytest.raises(prefs.InvalidValueError):
            prefs.validate_value("x", "bogus_leaf", 1)

    def test_str_list_too_long(self):
        doc = prefs.default_preferences()
        doc["culture"] = ["c"] * (prefs.MAX_LIST_LENGTH + 1)
        with pytest.raises(prefs.InvalidValueError):
            prefs.validate_document(doc)

    def test_float_map_not_a_mapping(self):
        doc = prefs.default_preferences()
        doc["priorities"] = "x"
        with pytest.raises(prefs.InvalidValueError):
            prefs.validate_document(doc)

    def test_float_map_too_many_keys(self):
        doc = prefs.default_preferences()
        doc["priorities"] = {f"k{i}": 0.5 for i in range(prefs.MAX_PRIORITIES + 1)}
        with pytest.raises(prefs.InvalidValueError):
            prefs.validate_document(doc)

    def test_validate_document_non_mapping(self):
        with pytest.raises(prefs.InvalidValueError):
            prefs.validate_document(None)

    def test_validate_document_nested_node_not_object(self):
        # All root keys present but a dict-spec node is not an object.
        doc = prefs.default_preferences()
        doc["compensation"] = "x"
        with pytest.raises(prefs.InvalidValueError):
            prefs.validate_document(doc)

    def test_validate_patch_not_object(self):
        with pytest.raises(prefs.AmbiguousPatchError):
            prefs.validate_patch("x")

    def test_validate_patch_unknown_top_key(self):
        with pytest.raises(prefs.AmbiguousPatchError):
            prefs.validate_patch({"set": {}, "bogus": 1})

    def test_validate_patch_set_not_object(self):
        with pytest.raises(prefs.AmbiguousPatchError):
            prefs.validate_patch({"set": "x"})

    def test_validate_patch_remove_not_object(self):
        with pytest.raises(prefs.AmbiguousPatchError):
            prefs.validate_patch({"remove": "x"})

    def test_remove_map_must_list_keys(self):
        with pytest.raises(prefs.AmbiguousPatchError):
            prefs.apply_patch(prefs.default_preferences(), {"remove": {"priorities": "x"}})

    def test_remove_subtree_must_use_null(self):
        with pytest.raises(prefs.AmbiguousPatchError):
            prefs.apply_patch(
                prefs.default_preferences(),
                {"remove": {"compensation": {"minimum_salary": 1}}},
            )

    def test_set_dynamic_inner_key_rejected(self):
        with pytest.raises(prefs.AmbiguousPatchError):
            prefs.apply_patch(
                prefs.default_preferences(), {"set": {"priorities.growth": 0.5}}
            )

    def test_remove_dynamic_entry(self):
        doc = prefs.apply_patch(
            prefs.default_preferences(), {"set": {"priorities": {"growth": 0.5}}}
        )[0]
        new, changes = prefs.apply_patch(doc, {"remove": {"priorities.growth": None}})
        assert changes == 1
        assert new["priorities"] == {}

    def test_subtree_set_missing_required_key(self):
        with pytest.raises(prefs.InvalidValueError):
            prefs.apply_patch(
                prefs.default_preferences(),
                {"set": {"work_location": {"modes": []}}},
            )

    def test_subtree_set_unknown_key(self):
        with pytest.raises(prefs.UnknownFieldError):
            prefs.apply_patch(
                prefs.default_preferences(),
                {"set": {"work_location": {
                    "modes": [], "countries": [], "require_onsite": None,
                    "max_in_office_days": None, "office_days_exact": None,
                    "bogus": 1,
                }}},
            )

    def test_subtree_set_non_object_value(self):
        with pytest.raises(prefs.InvalidValueError):
            prefs.apply_patch(prefs.default_preferences(), {"set": {"compensation": "x"}})

    def test_is_value_equal_one_side_none(self):
        doc = prefs.apply_patch(
            prefs.default_preferences(), {"set": {"compensation.minimum_salary": 150000}}
        )[0]
        new, _changes = prefs.apply_patch(
            doc, {"set": {"compensation.minimum_salary": None}}
        )
        assert new["compensation"]["minimum_salary"] is None

    def test_is_value_equal_float_compare(self):
        doc = prefs.apply_patch(
            prefs.default_preferences(), {"set": {"compensation.minimum_salary": 100000}}
        )[0]
        new, changes = prefs.apply_patch(
            doc, {"set": {"compensation.minimum_salary": 123456}}
        )
        assert changes == 1
        assert new["compensation"]["minimum_salary"] == 123456

    def test_markdown_equity_and_priorities(self):
        doc = prefs.apply_patch(
            prefs.default_preferences(),
            {"set": {"compensation.equity_minimum_percent": 0.05,
                     "priorities": {"growth": 0.6, "remote": 0.4}}},
        )[0]
        md = prefs.to_markdown(doc)
        assert "Minimum equity target: 0.1%" in md
        assert "## Priorities" in md
        assert "- growth: 0.60" in md
        assert "- remote: 0.40" in md

    def test_normalize_ts_none(self):
        assert prefs._normalize_ts(None) is None
        # Not a string and no tzinfo -> None.
        assert prefs._normalize_ts(123) is None

    def test_remove_float_map_key_list_form(self):
        doc = prefs.apply_patch(
            prefs.default_preferences(), {"set": {"priorities": {"a": 0.5, "b": 0.4}}}
        )[0]
        new, changes = prefs.apply_patch(doc, {"remove": {"priorities": ["a"]}})
        assert changes == 1
        assert new["priorities"] == {"b": 0.4}

    def test_is_value_equal_single_none(self):
        assert prefs._is_value_equal("int", None, 5) is False
        assert prefs._is_value_equal("int", 5, None) is False
        assert prefs._is_value_equal("int", None, None) is True

    def test_remove_str_list_depth1(self):
        doc = prefs.default_preferences()
        doc = prefs.apply_patch(doc, {"set": {"culture": ["a", "b"]}})[0]
        new, changes = prefs.apply_patch(doc, {"remove": {"culture": ["a"]}})
        assert changes == 1
        assert new["culture"] == ["b"]

    @pytest.mark.django_db
    def test_stale_check_iso_string_naive_and_tzaware(self):
        from django.utils import timezone as tz
        u = get_user_model().objects.create_user(username="edge-ts", password="x")
        prefs.apply_patch_to_user(u, {"set": {"notes": "first"}})
        # naive ISO string (parses -> made aware) -> mismatch
        with pytest.raises(prefs.StalePreferenceError):
            prefs.apply_patch_to_user(u, {"set": {"notes": "s"}}, expected_modified="2026-01-01T00:00:00")
        # tz-aware datetime object -> tzinfo path
        with pytest.raises(prefs.StalePreferenceError):
            prefs.apply_patch_to_user(u, {"set": {"notes": "s"}}, expected_modified=tz.now())

    @pytest.mark.django_db
    def test_stale_check_invalid_iso_string(self):
        u = get_user_model().objects.create_user(username="edge-ts2", password="x")
        prefs.apply_patch_to_user(u, {"set": {"notes": "first"}})
        with pytest.raises(prefs.StalePreferenceError):
            prefs.apply_patch_to_user(u, {"set": {"notes": "s"}}, expected_modified="not-a-date")


# ---------------------------------------------------------------------------
# Propose / diff / revision / undo lifecycle (issue #466)
# ---------------------------------------------------------------------------
class TestDiffPatch:
    def test_diff_scalar_int(self):
        changes, count = prefs.diff_patch(
            prefs.default_preferences(),
            {"set": {"compensation.minimum_salary": 150000}},
        )
        assert count == 1
        assert changes == [{
            "path": "compensation.minimum_salary",
            "old": None,
            "new": 150000,
        }]

    def test_diff_dedupes_dynamic_map_keys_to_one_parent_anchor(self):
        # Two dynamic float_map keys in one patch collapse to the single
        # set-able parent anchor; the second visit hits the seen-continue.
        doc = prefs.default_preferences()
        doc["priorities"] = {"comp": 0.9, "culture": 0.5, "growth": 0.25}
        changes, count = prefs.diff_patch(
            doc,
            {"remove": {"priorities.comp": None, "priorities.culture": None}},
        )
        assert count == 2
        assert changes == [{
            "path": "priorities",
            "old": {"comp": 0.9, "culture": 0.5, "growth": 0.25},
            "new": {"growth": 0.25},
        }]

    def test_build_undo_token_returns_none_for_empty_changes(self):
        assert prefs.build_undo_token(7, None) is None

    def test_check_stale_revision_noops_when_expected_revision_is_none(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        pref = UserPreference.objects.get(user=user)
        # Defensive guard: the precondition is skipped entirely.
        prefs._check_stale_revision(pref, None)

    def test_diff_float_bool_str(self):
        doc = prefs.default_preferences()
        changes, count = prefs.diff_patch(doc, {"set": {
            "compensation.equity_minimum_percent": 2.5,
            "work_location.require_onsite": True,
            "notes": "remote only",
        }})
        assert count == 3
        by_path = {c["path"]: c for c in changes}
        assert by_path["compensation.equity_minimum_percent"]["new"] == 2.5
        assert by_path["work_location.require_onsite"] == {
            "path": "work_location.require_onsite", "old": None, "new": True,
        }
        assert by_path["notes"]["old"] == ""

    def test_diff_str_list_and_remove_items(self):
        doc = prefs.apply_patch(
            prefs.default_preferences(), {"set": {"culture": ["a", "b", "c"]}}
        )[0]
        changes, count = prefs.diff_patch(doc, {"remove": {"culture": ["a", "c"]}})
        assert count == 2  # apply_patch's per-item count
        assert changes == [{
            "path": "culture", "old": ["a", "b", "c"], "new": ["b"],
        }]

    def test_diff_float_map_and_dynamic_key_remove(self):
        doc = prefs.apply_patch(
            prefs.default_preferences(),
            {"set": {"priorities": {"comp": 0.9, "culture": 0.3}}},
        )[0]
        changes, count = prefs.diff_patch(doc, {"remove": {"priorities.comp": None}})
        assert count == 1
        # Dynamic keys collapse to the set-able parent map path.
        assert changes == [{
            "path": "priorities",
            "old": {"comp": 0.9, "culture": 0.3},
            "new": {"culture": 0.3},
        }]

    def test_diff_whole_subtree_set(self):
        doc = prefs.default_preferences()
        subtree = copy.deepcopy(doc["work_location"])
        subtree["modes"] = ["remote"]
        changes, count = prefs.diff_patch(doc, {"set": {"work_location": subtree}})
        assert count == 1
        assert changes[0]["path"] == "work_location"
        assert changes[0]["new"]["modes"] == ["remote"]

    def test_diff_subtree_reset_remove(self):
        doc = prefs.apply_patch(
            prefs.default_preferences(), {"set": {"geography.regions": ["EMEA"]}}
        )[0]
        changes, count = prefs.diff_patch(doc, {"remove": {"geography": None}})
        assert count == 1
        assert changes[0]["path"] == "geography"
        assert changes[0]["old"]["regions"] == ["EMEA"]
        assert changes[0]["new"]["regions"] == []

    def test_diff_noop_is_empty(self):
        changes, count = prefs.diff_patch(
            prefs.default_preferences(), {"set": {"notes": ""}}
        )
        assert count == 0
        assert changes == []

    def test_diff_invalid_patch_raises_same_errors(self):
        with pytest.raises(UnknownFieldError):
            prefs.diff_patch(prefs.default_preferences(), {"set": {"bogus": 1}})
        with pytest.raises(InvalidValueError):
            prefs.diff_patch(
                prefs.default_preferences(),
                {"set": {"compensation.minimum_salary": -1}},
            )
        with pytest.raises(AmbiguousPatchError):
            prefs.diff_patch(prefs.default_preferences(), {})


class TestPropose:
    def _snapshot(self, user):
        row = UserPreference.objects.filter(user=user).first()
        audits = UserPreferenceAudit.objects.count()
        if row is None:
            return None, audits
        return (row.modified, row.revision, copy.deepcopy(row.preferences)), audits

    def test_propose_writes_nothing_valid_patch(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        before = self._snapshot(user)
        result = prefs.propose_patch_for_user(
            user, {"set": {"compensation.minimum_salary": 200000}}
        )
        assert self._snapshot(user) == before
        assert result["base_revision"] == 1  # the seed apply advanced it
        assert result["change_count"] == 1
        assert result["scope"] == "account"
        assert result["changes"][0]["new"] == 200000
        assert "unsupported_criteria" in result

    def test_propose_writes_nothing_and_no_row_created(self, user):
        before = self._snapshot(user)
        result = prefs.propose_patch_for_user(user, {"set": {"notes": "hi"}})
        assert self._snapshot(user) == before  # still no row, no audit rows
        assert result["base_revision"] == 0
        assert result["base_modified"] is None

    @pytest.mark.parametrize("patch,exc", [
        ({"set": {"bogus": 1}}, UnknownFieldError),
        ({"set": {"compensation.minimum_salary": -5}}, InvalidValueError),
        ({}, AmbiguousPatchError),
    ])
    def test_propose_invalid_patch_writes_nothing(self, user, patch, exc):
        prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        before = self._snapshot(user)
        with pytest.raises(exc):
            prefs.propose_patch_for_user(user, patch)
        assert self._snapshot(user) == before

    def test_propose_change_count_matches_apply_patch(self, user):
        patch = {"set": {"culture": ["a", "b"]}, "remove": {"notes": None}}
        doc = prefs.default_preferences()
        expected_count = prefs.apply_patch(doc, patch)[1]
        result = prefs.propose_patch_for_user(user, patch)
        assert result["change_count"] == expected_count

    def test_propose_noop_patch(self, user):
        result = prefs.propose_patch_for_user(user, {"set": {"notes": ""}})
        assert result["change_count"] == 0
        assert result["changes"] == []

    def test_propose_unknown_scope_rejected(self, user):
        with pytest.raises(AmbiguousPatchError):
            prefs.propose_patch_for_user(user, {"set": {"notes": "x"}}, scope="bogus")


class TestScopeSearch:
    def test_effective_document_not_persisted(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "canonical"}})
        patch = {"set": {"work_location.modes": ["remote"]}}
        result = prefs.propose_patch_for_user(user, patch, scope="search")
        effective = result["effective_document"]
        assert effective["work_location"]["modes"] == ["remote"]
        row = UserPreference.objects.get(user=user)
        assert row.preferences["work_location"]["modes"] == []
        assert row.revision == 1  # only the seed apply advanced it
        assert UserPreferenceAudit.objects.count() == 2  # only the seed apply
        export = prefs.export(user)
        assert export["preferences"]["work_location"]["modes"] == []

    def test_effective_document_helper(self, user):
        effective = prefs.effective_document(user, {"set": {"industry": ["ai"]}})
        assert effective["industry"] == ["ai"]
        assert not UserPreference.objects.filter(user=user).exists()


class TestRevisionApply:
    def test_apply_advances_revision_by_one(self, user):
        result = prefs.apply_patch_to_user(user, {"set": {"notes": "one"}})
        assert result["changed"] is True
        assert result["revision"] == 1
        assert UserPreference.objects.get(user=user).revision == 1
        result2 = prefs.apply_patch_to_user(user, {"set": {"notes": "two"}})
        assert result2["revision"] == 2

    def test_noop_apply_leaves_revision_modified_audit(self, user):
        first = prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        row = UserPreference.objects.get(user=user)
        modified_before = row.modified
        audits_before = UserPreferenceAudit.objects.count()
        again = prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        row.refresh_from_db()
        assert again["changed"] is False
        assert again["revision"] == 1
        assert again["undo"] is None
        assert again["change_id"] is None
        assert again["changes"] == []
        assert row.revision == 1
        assert row.modified == modified_before
        assert UserPreferenceAudit.objects.count() == audits_before
        assert first["change_id"] == f"{user.pk}:1"

    def test_expected_revision_mismatch_raises_and_writes_nothing(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "one"}})
        before = UserPreference.objects.get(user=user).preferences
        audits = UserPreferenceAudit.objects.count()
        with pytest.raises(StalePreferenceError) as excinfo:
            prefs.apply_patch_to_user(
                user, {"set": {"notes": "two"}}, expected_revision=7
            )
        assert excinfo.value.current_revision == 1
        row = UserPreference.objects.get(user=user)
        assert row.preferences == before
        assert row.revision == 1
        assert UserPreferenceAudit.objects.count() == audits

    def test_expected_revision_match_applies(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "one"}})
        result = prefs.apply_patch_to_user(
            user, {"set": {"notes": "two"}}, expected_revision=1
        )
        assert result["revision"] == 2

    def test_expected_revision_zero_allows_absent_row(self, user):
        result = prefs.apply_patch_to_user(
            user, {"set": {"notes": "x"}}, expected_revision=0
        )
        assert result["revision"] == 1

    def test_expected_revision_nonzero_absent_row_stale(self, user):
        with pytest.raises(StalePreferenceError) as excinfo:
            prefs.apply_patch_to_user(
                user, {"set": {"notes": "x"}}, expected_revision=3
            )
        assert excinfo.value.current_revision == 0
        assert not UserPreference.objects.filter(user=user).exists()

    def test_both_preconditions_revision_wins(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "one"}})
        row = UserPreference.objects.get(user=user)
        stale_ts = "2020-01-01T00:00:00Z"
        # Matching revision + mismatching timestamp -> applies (timestamp ignored).
        result = prefs.apply_patch_to_user(
            user, {"set": {"notes": "two"}},
            expected_modified=stale_ts, expected_revision=row.revision,
        )
        assert result["revision"] == 2
        # Mismatching revision + matching timestamp -> stale.
        row.refresh_from_db()
        with pytest.raises(StalePreferenceError):
            prefs.apply_patch_to_user(
                user, {"set": {"notes": "three"}},
                expected_modified=row.modified, expected_revision=99,
            )

    def test_expected_modified_legacy_behaviour_unchanged(self, user):
        # Old-caller path: no expected_revision anywhere.
        result = prefs.apply_patch_to_user(user, {"set": {"notes": "first"}})
        modified = result["modified"]
        ok = prefs.apply_patch_to_user(
            user, {"set": {"notes": "second"}}, expected_modified=modified
        )
        assert ok["changed"] is True
        with pytest.raises(StalePreferenceError):
            prefs.apply_patch_to_user(
                user, {"set": {"notes": "third"}}, expected_modified=modified
            )

    def test_expected_modified_absent_sentinel_unchanged(self, user):
        # Row created mid-turn -> PREFERENCE_ABSENT caller goes stale.
        prefs.apply_patch_to_user(user, {"set": {"notes": "raced"}})
        with pytest.raises(StalePreferenceError):
            prefs.apply_patch_to_user(
                user, {"set": {"notes": "late"}},
                expected_modified=prefs.PREFERENCE_ABSENT,
            )
        # Row deleted mid-turn -> fail closed.
        other = get_user_model().objects.create_user(username="gone", password="x")
        prefs.apply_patch_to_user(other, {"set": {"notes": "seed"}})
        ts = UserPreference.objects.get(user=other).modified
        prefs.delete_user_preference(other)
        with pytest.raises(StalePreferenceError):
            prefs.apply_patch_to_user(
                other, {"set": {"notes": "revive"}}, expected_modified=ts
            )


class TestStaleCarriesCurrentRevision:
    """AC-5 (issue #466 review round 3): every ``StalePreferenceError`` —
    revision path and legacy timestamp/absent path alike — carries a
    machine-readable ``current_revision`` so the 409 envelope can offer a
    fresh review path without a second read."""

    def test_absent_sentinel_created_mid_turn_carries_current_revision(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "raced"}})
        revision = UserPreference.objects.get(user=user).revision
        before = UserPreference.objects.get(user=user).preferences
        with pytest.raises(StalePreferenceError) as excinfo:
            prefs.apply_patch_to_user(
                user, {"set": {"notes": "late"}},
                expected_modified=prefs.PREFERENCE_ABSENT,
            )
        assert excinfo.value.current_revision == revision
        # Nothing was written.
        assert UserPreference.objects.get(user=user).preferences == before

    def test_invalid_timestamp_carries_current_revision(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        revision = UserPreference.objects.get(user=user).revision
        with pytest.raises(StalePreferenceError) as excinfo:
            prefs.apply_patch_to_user(
                user, {"set": {"notes": "s"}}, expected_modified="not-a-date"
            )
        assert excinfo.value.current_revision == revision

    def test_timestamp_mismatch_carries_current_revision(self, user):
        first = prefs.apply_patch_to_user(user, {"set": {"notes": "one"}})
        second = prefs.apply_patch_to_user(user, {"set": {"notes": "two"}})
        with pytest.raises(StalePreferenceError) as excinfo:
            prefs.apply_patch_to_user(
                user, {"set": {"notes": "three"}},
                expected_modified=first["modified"],
            )
        assert excinfo.value.current_revision == second["revision"]
        assert UserPreference.objects.get(user=user).preferences["notes"] == "two"

    def test_deleted_mid_turn_legacy_path_carries_zero(self, user):
        # The row is gone at apply time, so there is no live revision to
        # carry: 0 signals "no committed revision", matching the other
        # deleted-row raises.
        prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        ts = UserPreference.objects.get(user=user).modified
        prefs.delete_user_preference(user)
        with pytest.raises(StalePreferenceError) as excinfo:
            prefs.apply_patch_to_user(
                user, {"set": {"notes": "revive"}}, expected_modified=ts
            )
        assert excinfo.value.current_revision == 0
        assert not UserPreference.objects.filter(user=user).exists()


class TestUndo:
    def test_apply_result_carries_changes_undo_change_id(self, user):
        result = prefs.apply_patch_to_user(
            user, {"set": {"compensation.minimum_salary": 180000}}
        )
        assert result["changes"] == [{
            "path": "compensation.minimum_salary", "old": None, "new": 180000,
        }]
        assert result["undo"] == {
            "expected_revision": 1,
            "document": prefs.default_preferences(),
        }
        assert result["change_id"] == f"{user.pk}:1"

    def test_undo_restores_document_byte_for_byte(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "seed", "culture": ["x"]}})
        before = copy.deepcopy(UserPreference.objects.get(user=user).preferences)
        applied = prefs.apply_patch_to_user(
            user,
            {"set": {"notes": "edited"}, "remove": {"culture": ["x"]}},
        )
        restored = prefs.undo_preference_change(user, applied["undo"])
        assert restored["changed"] is True
        row = UserPreference.objects.get(user=user)
        assert row.preferences == before
        assert row.revision == 3

    def test_undo_after_intervening_edit_stale_and_writes_nothing(self, user):
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "v1"}})
        prefs.apply_patch_to_user(user, {"set": {"notes": "v2"}})
        before = copy.deepcopy(UserPreference.objects.get(user=user).preferences)
        audits = UserPreferenceAudit.objects.count()
        with pytest.raises(StalePreferenceError) as excinfo:
            prefs.undo_preference_change(user, applied["undo"])
        assert excinfo.value.current_revision == 2
        row = UserPreference.objects.get(user=user)
        assert row.preferences == before
        assert UserPreferenceAudit.objects.count() == audits

    def test_undo_none_token_raises_ambiguous(self, user):
        result = prefs.apply_patch_to_user(user, {"set": {"notes": ""}})
        assert result["undo"] is None
        with pytest.raises(AmbiguousPatchError):
            prefs.undo_preference_change(user, None)
        with pytest.raises(AmbiguousPatchError):
            prefs.undo_preference_change(user, {"patch": {"set": {}}})

    def test_undo_round_trips_list_remove_order(self, user):
        prefs.apply_patch_to_user(
            user, {"set": {"culture": ["a", "b", "c", "d"]}}
        )
        applied = prefs.apply_patch_to_user(
            user, {"remove": {"culture": ["b", "d"]}}
        )
        assert UserPreference.objects.get(user=user).preferences["culture"] == ["a", "c"]
        prefs.undo_preference_change(user, applied["undo"])
        assert UserPreference.objects.get(user=user).preferences["culture"] == [
            "a", "b", "c", "d",
        ]

    def test_undo_round_trips_dynamic_priority_remove(self, user):
        prefs.apply_patch_to_user(
            user, {"set": {"priorities": {"comp": 0.9, "culture": 0.3}}}
        )
        applied = prefs.apply_patch_to_user(
            user, {"remove": {"priorities.comp": None}}
        )
        # The token captures the full pre-apply document (byte-identical
        # restore, issue #466 review).
        assert applied["undo"]["document"]["priorities"] == {
            "comp": 0.9, "culture": 0.3,
        }
        prefs.undo_preference_change(user, applied["undo"])
        assert UserPreference.objects.get(user=user).preferences["priorities"] == {
            "comp": 0.9, "culture": 0.3,
        }


@pytest.mark.django_db
class TestExpectedRevisionValidation:
    """Issue #466 review: a tampered/null/missing revision precondition must
    never silently downgrade to the unchecked legacy path."""

    def test_apply_rejects_bool_revision(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        with pytest.raises(InvalidValueError):
            prefs.apply_patch_to_user(
                user, {"set": {"notes": "x"}}, expected_revision=True
            )

    def test_apply_rejects_string_and_negative_revision(self, user):
        prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        for bad in ("1", -1, 1.5):
            with pytest.raises(InvalidValueError):
                prefs.apply_patch_to_user(
                    user, {"set": {"notes": "x"}}, expected_revision=bad
                )

    def test_undo_rejects_null_or_bool_revision(self, user):
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        token = applied["undo"]
        for bad in (None, True, "1", -1):
            with pytest.raises(AmbiguousPatchError):
                prefs.undo_preference_change(
                    user, dict(token, expected_revision=bad)
                )
        # A tampered token never followed the legacy no-precondition path.
        assert UserPreference.objects.get(user=user).preferences["notes"] == "seed"

    def test_undo_rejects_missing_or_malformed_document(self, user):
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        with pytest.raises(AmbiguousPatchError):
            prefs.undo_preference_change(user, {"expected_revision": 1})
        with pytest.raises(AmbiguousPatchError):
            prefs.undo_preference_change(
                user, {"expected_revision": 1, "document": "not-a-dict"}
            )

    def test_undo_rejects_oversized_document(self, user):
        """A token whose captured document exceeds the serialized-size
        ceiling is rejected before any store access (issue #466 review)."""
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        token = dict(applied["undo"])
        token["document"] = dict(token["document"], notes="x" * (65 * 1024))
        with pytest.raises(AmbiguousPatchError):
            prefs.undo_preference_change(user, token)

    def test_undo_rejects_non_serializable_document(self, user):
        """A token whose document cannot be JSON-serialized at all is
        rejected, never reaching the row lock (issue #466 review)."""
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        token = dict(applied["undo"])
        token["document"] = dict(token["document"], notes=object())
        with pytest.raises(AmbiguousPatchError):
            prefs.undo_preference_change(user, token)

    def test_undo_rejects_corrupt_known_value(self, user):
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        token = dict(applied["undo"])
        document = copy.deepcopy(token["document"])
        document["culture"] = "not-a-list"
        with pytest.raises(InvalidValueError):
            prefs.undo_preference_change(
                user, dict(token, document=document)
            )

    def test_undo_rejects_non_dict_known_subtree(self, user):
        """A tampered token whose known subtree is not a JSON object is
        rejected by the stored-shape check (issue #466 review)."""
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        token = dict(applied["undo"])
        document = copy.deepcopy(token["document"])
        document["compensation"] = "not-a-dict"
        with pytest.raises(InvalidValueError):
            prefs.undo_preference_change(
                user, dict(token, document=document)
            )

    def test_undo_after_row_deleted_is_stale_not_recreate(self, user):
        """An undo landing after the preference row was deleted fails closed
        with StalePreferenceError (current_revision 0) instead of silently
        re-creating deleted state (issue #466 review)."""
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        token = applied["undo"]
        UserPreference.objects.filter(user=user).delete()
        with pytest.raises(prefs.StalePreferenceError) as exc_info:
            prefs.undo_preference_change(user, token)
        assert exc_info.value.current_revision == 0
        assert not UserPreference.objects.filter(user=user).exists()


@pytest.mark.django_db
class TestRevisionLifecycle:
    """Issue #466 review: every committed canonical document change advances
    the monotonic revision, so an old undo token can never overwrite a reset
    or a delete/recreate."""

    def test_reset_advances_revision_and_invalidates_undo(self, user):
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        token = applied["undo"]
        result = prefs.reset(user)
        assert result["revision"] == 2
        with pytest.raises(StalePreferenceError) as excinfo:
            prefs.undo_preference_change(user, token)
        assert excinfo.value.current_revision == 2
        assert UserPreference.objects.get(user=user).preferences["notes"] == ""

    def test_idempotent_reset_does_not_advance_revision(self, user):
        prefs.reset(user)
        pref = UserPreference.objects.get(user=user)
        assert pref.revision == 0

    def test_delete_recreate_continues_revision_lineage(self, user):
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        token = applied["undo"]  # expected_revision == 1
        prefs.delete_user_preference(user)
        # Re-create on next interaction: the row must NOT restart at 0, or
        # the old token could match once the new row climbs back to 1.
        prefs.read(user)
        pref = UserPreference.objects.get(user=user)
        assert pref.revision == 2  # delete's floor (1 + 1), not a restart
        # The old token's precondition never matches the re-created lineage.
        with pytest.raises(StalePreferenceError):
            prefs.undo_preference_change(user, token)
        # A fresh apply advances from the floor.
        result = prefs.apply_patch_to_user(user, {"set": {"notes": "new"}})
        assert result["revision"] == 3

    def test_first_interaction_still_starts_at_zero(self, user):
        prefs.read(user)
        assert UserPreference.objects.get(user=user).revision == 0


@pytest.mark.django_db
class TestUndoByteIdenticalShapes:
    """Issue #466 review: undo restores the prior document byte-identically
    across every supported stored shape, and additive unknown nested keys
    ride along through propose/apply/undo untouched."""

    def _store_document(self, user, document):
        pref = UserPreference.objects.get(user=user)
        pref.preferences = document
        pref.save(update_fields=["preferences"])
        return pref

    def test_undo_restores_pre_v3_shape_byte_identically(self, user):
        prefs.read(user)
        # A pre-v3 stored document: no roles/importance/scope and no nested
        # v3 keys (e.g. work_location.office_days_exact).
        legacy = {
            "compensation": {
                "minimum_salary": 150000,
                "currency": "USD",
                "equity_minimum_percent": None,
                "require_public_company": None,
                "basis": "base",
                "period": "year",
            },
            "culture": ["remote-first"],
            "work_location": {"modes": ["remote"], "countries": [],
                              "require_onsite": None, "max_in_office_days": None},
            "geography": {"regions": [], "remote_friendly": None},
            "industry": [],
            "funding_stage": [],
            "vesting": {"max_cliff_months": None, "max_vesting_months": None,
                        "prefer_accelerated": None},
            "exclusions": {"companies": [], "titles": [], "industries": [],
                           "locations": []},
            "priorities": {},
            "notes": "legacy note",
        }
        self._store_document(user, legacy)
        # A notes-only apply backfills the v3 keys on the stored document.
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "edited"}})
        row = UserPreference.objects.get(user=user)
        assert "roles" in row.preferences  # backfilled by apply_patch
        # Undo restores the exact pre-v3 shape — no backfilled keys remain.
        restored = prefs.undo_preference_change(user, applied["undo"])
        assert restored["changed"] is True
        row.refresh_from_db()
        assert row.preferences == legacy

    def test_subtree_set_preserves_unknown_nested_keys(self, user):
        prefs.read(user)
        document = prefs.default_preferences()
        document["compensation"]["future_additive"] = {"nested": True}
        self._store_document(user, document)
        new_comp = copy.deepcopy(document["compensation"])
        del new_comp["future_additive"]
        new_comp["minimum_salary"] = 200000
        applied = prefs.apply_patch_to_user(
            user, {"set": {"compensation": new_comp}}
        )
        row = UserPreference.objects.get(user=user)
        # The subtree replace preserved the additive nested key.
        assert row.preferences["compensation"]["future_additive"] == {"nested": True}
        assert row.preferences["compensation"]["minimum_salary"] == 200000
        # Undo restores the prior document byte-identically, additive key and all.
        prefs.undo_preference_change(user, applied["undo"])
        row.refresh_from_db()
        assert row.preferences == document

    def test_undo_restores_document_with_unknown_top_level_keys(self, user):
        prefs.read(user)
        document = prefs.default_preferences()
        document["future_section"] = {"anything": [1, 2, 3]}
        self._store_document(user, document)
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "edited"}})
        prefs.undo_preference_change(user, applied["undo"])
        row = UserPreference.objects.get(user=user)
        assert row.preferences == document

    def test_undo_noop_when_document_already_matches(self, user):
        applied = prefs.apply_patch_to_user(user, {"set": {"notes": "seed"}})
        token = applied["undo"]
        # Manually restore the document without advancing the revision, so
        # the undo precondition matches but nothing differs.
        pref = UserPreference.objects.get(user=user)
        pref.preferences = copy.deepcopy(token["document"])
        pref.save(update_fields=["preferences"])
        audits = UserPreferenceAudit.objects.count()
        result = prefs.undo_preference_change(user, token)
        assert result["changed"] is False
        assert result["revision"] == 1
        assert UserPreferenceAudit.objects.count() == audits


@pytest.mark.django_db(transaction=True)
class TestRecomputeHook:
    @pytest.fixture
    def captured(self, settings, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "crank.tests.services.test_preferences._record_hook", calls.append,
            raising=False,
        )
        settings.PREFERENCE_RECOMPUTE_HOOK = (
            "crank.tests.services.test_preferences._record_hook"
        )
        # Attach a module-level callable the dotted path resolves to.
        import crank.tests.services.test_preferences as this_module
        this_module._hook_calls = calls
        return calls

    def test_hook_fires_once_with_change_id(self, user, captured):
        prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        assert captured == [f"{user.pk}:1"]

    def test_hook_not_fired_for_noop(self, user, captured):
        prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        captured.clear()
        prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        assert captured == []

    def test_hook_not_fired_on_rollback(self, user, captured):
        from django.db import transaction as tx

        class _Boom(Exception):
            pass

        with pytest.raises(_Boom):
            with tx.atomic():
                prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
                raise _Boom()
        assert captured == []
        assert not UserPreference.objects.filter(user=user).exists()

    def test_hook_observes_committed_revision(self, user, settings):
        seen = []
        settings.PREFERENCE_RECOMPUTE_HOOK = (
            "crank.tests.services.test_preferences._revision_observing_hook"
        )
        import crank.tests.services.test_preferences as this_module
        this_module._revision_observer = (user.pk, seen)
        prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        prefs.apply_patch_to_user(user, {"set": {"notes": "y"}})
        assert seen == [1, 2]

    def test_hook_exception_swallowed(self, user, settings, caplog):
        settings.PREFERENCE_RECOMPUTE_HOOK = (
            "crank.tests.services.test_preferences._exploding_hook"
        )
        with caplog.at_level("ERROR", logger="crank.services.preferences"):
            result = prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        assert result["changed"] is True  # save never depends on the hook
        assert "preference recompute hook failed" in caplog.text

    def test_empty_hook_is_noop(self, user, settings):
        settings.PREFERENCE_RECOMPUTE_HOOK = ""
        result = prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        assert result["changed"] is True

    def test_default_hook_path_resolves_and_noops_with_flags_off(self, user, settings):
        """issue #475: the default PREFERENCE_RECOMPUTE_HOOK points at the
        durable recompute consumer's fast path, which itself no-ops unless
        MATCH_RECOMPUTE_ENABLED and the switch are both on."""
        settings.PREFERENCE_RECOMPUTE_HOOK = (
            "crank.services.match_recompute.on_preference_committed"
        )
        settings.MATCH_RECOMPUTE_ENABLED = False
        result = prefs.apply_patch_to_user(user, {"set": {"notes": "x"}})
        assert result["changed"] is True
        from crank.models.job_match import MatchResultState

        assert not MatchResultState.objects.filter(user=user).exists()


class TestNoContentsLogging:
    def test_changes_undo_never_logged(self, user, caplog):
        secret = "s3kr3t-notes-payload"
        with caplog.at_level("DEBUG"):
            result = prefs.apply_patch_to_user(user, {"set": {"notes": secret}})
            prefs.undo_preference_change(user, result["undo"])
            prefs.propose_patch_for_user(user, {"set": {"notes": "other"}})
        for record in caplog.records:
            assert secret not in record.getMessage()
            assert "undo" not in record.getMessage().lower()


def _record_hook(change_id):
    import crank.tests.services.test_preferences as this_module
    this_module._hook_calls.append(change_id)


def _revision_observing_hook(change_id):
    from crank.models.preference import UserPreference as _UP
    import crank.tests.services.test_preferences as this_module
    user_pk, seen = this_module._revision_observer
    user_id, revision = change_id.split(":")
    assert int(user_id) == user_pk
    # The hook fires after commit: the stored row already carries the revision.
    assert _UP.objects.get(user_id=user_id).revision == int(revision)
    seen.append(int(revision))


def _exploding_hook(change_id):
    raise RuntimeError("hook exploded")
