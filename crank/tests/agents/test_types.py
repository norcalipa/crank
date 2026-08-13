# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Unit tests for the schema-validated job-search result types."""
import pytest

from crank.agents.job_search.errors import InvalidModelOutputError
from crank.agents.job_search.types import AssistantCompletion


def _completion(**overrides):
    payload = {
        "message": "Consider Acme.",
        "cited_organization_ids": [1, 2],
        "cited_job_listing_ids": [],
        "preference_patch": None,
    }
    payload.update(overrides)
    return payload


class TestPreferencePatchLeafValidation:
    def test_dict_boolean_leaf_rejected(self):
        """MAJOR-2: booleans rejected in dicts just like in sequences."""
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(preference_patch={"remote_ok": True})
            )

    def test_sequence_boolean_leaf_rejected(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(preference_patch={"tags": ["x", False]})
            )

    def test_nested_dict_boolean_leaf_rejected(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(preference_patch={"nested": {"flag": True}})
            )

    def test_non_serializable_leaf_rejected(self):
        """MAJOR-1: un-serializable scalar leaves are type-checked in dicts too."""
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(preference_patch={"bad": {1, 2, 3}})
            )

    def test_string_number_null_leaves_allowed(self):
        result = AssistantCompletion.from_json(
            _completion(
                preference_patch={
                    "region": "bay-area",
                    "min_score": 4.2,
                    "max_results": 10,
                    "note": None,
                }
            )
        )
        assert result.has_preference_patch
        assert result.preference_patch["region"] == "bay-area"
        assert result.preference_patch["min_score"] == 4.2
        assert result.preference_patch["max_results"] == 10
        assert result.preference_patch["note"] is None

    def test_empty_patch_allowed(self):
        result = AssistantCompletion.from_json(_completion(preference_patch={}))
        assert result.has_preference_patch
        assert result.preference_patch == {}

    def test_non_string_dict_key_rejected(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(_completion(preference_patch={1: "x"}))

    def test_dict_inside_list_validated(self):
        # A dict leaf nested inside a list must still be type-checked.
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(preference_patch={"rows": [{"flag": True}]})
            )

    def test_list_inside_list_validated(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(preference_patch={"matrix": [["x", False]]})
            )

    def test_oversize_nesting_rejected(self):
        deep: dict = {"a": {}}
        node = deep["a"]
        for _ in range(9):
            node["b"] = {}
            node = node["b"]
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(_completion(preference_patch=deep))

    def test_oversize_sequence_nesting_rejected(self):
        deep: list = ["x"]
        for _ in range(9):
            deep = [deep]
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(preference_patch={"matrix": deep})
            )


class TestFromJsonTopLevel:
    def test_invalid_json_string_rejected(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json("{not json")

    def test_non_dict_payload_rejected(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json([1, 2, 3])

    def test_missing_required_keys_rejected(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json({"message": "hi"})

    def test_empty_message_rejected(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(message="   ")
            )

    def test_too_many_cited_organizations_rejected(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(cited_organization_ids=list(range(201)))
            )

    def test_non_integer_cited_id_rejected(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(cited_organization_ids=[1, "2"])
            )

    def test_duplicate_cited_ids_rejected(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(cited_organization_ids=[1, 1])
            )

    def test_non_dict_preference_patch_rejected(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                _completion(preference_patch="remote_ok")
            )

    def test_cited_job_listing_ids_must_be_list(self):
        with pytest.raises(InvalidModelOutputError, match="cited_job_listing_ids"):
            AssistantCompletion.from_json(
                _completion(cited_job_listing_ids="not-a-list")
            )

    def test_cited_job_listing_ids_must_be_integers(self):
        with pytest.raises(InvalidModelOutputError, match="cited_job_listing_ids must be integers"):
            AssistantCompletion.from_json(
                _completion(cited_job_listing_ids=[1, "two"])
            )

    def test_cited_job_listing_ids_must_be_unique(self):
        with pytest.raises(InvalidModelOutputError, match="cited_job_listing_ids must be unique"):
            AssistantCompletion.from_json(
                _completion(cited_job_listing_ids=[1, 1])
            )

    def test_too_many_cited_job_listings_rejected(self):
        with pytest.raises(InvalidModelOutputError, match="cites more than"):
            AssistantCompletion.from_json(
                _completion(cited_job_listing_ids=list(range(201)))
            )

    def test_cited_job_listing_ids_bool_rejected(self):
        with pytest.raises(InvalidModelOutputError, match="must be integers"):
            AssistantCompletion.from_json(
                _completion(cited_job_listing_ids=[True])
            )
