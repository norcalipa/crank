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
            ("priorities", {"culture": "high"}),
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
            {"set": {"compensation": {"minimum_salary": 200000, "currency": "USD", "equity_minimum_percent": 0.5}}},
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
        noop, c3 = prefs.apply_patch(first, {"remove": {"culture": ["missing"]}})
        assert c3 == 0

    def test_apply_patch_does_not_mutate_input(self):
        doc = prefs.default_preferences()
        original = copy.deepcopy(doc)
        new, _ = prefs.apply_patch(doc, {"set": {"culture": ["x"]}})
        assert doc == original
        assert new != original


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