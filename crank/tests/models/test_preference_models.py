# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from crank.models.preference import SCHEMA_VERSION, UserPreference, UserPreferenceAudit
from crank.services.preferences import default_preferences


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(
        username="carol", password="pw", email="carol@example.com"
    )


class TestUserPreferenceModel:
    def test_one_preference_per_user(self, user):
        UserPreference.objects.create(user=user, preferences=default_preferences())
        with pytest.raises(IntegrityError):
            UserPreference.objects.create(user=user, preferences=default_preferences())

    def test_defaults_are_valid_schema_v1(self, user):
        pref = UserPreference.objects.create(user=user, preferences=default_preferences())
        assert pref.preferences == default_preferences()
        assert pref.schema_version == SCHEMA_VERSION
        # created/modified are set by TimeStampedModel.
        assert pref.created is not None
        assert pref.modified is not None

    def test_invalid_structure_default_rejected_by_service(self, user):
        pref = UserPreference.objects.create(user=user, preferences={"bogus": 1})
        # The service layer is the gatekeeper: an invalid document is rejected.
        from crank.services.preferences import InvalidValueError, validate_document

        with pytest.raises(InvalidValueError):
            validate_document(pref.preferences)

    def test_cascade_delete_from_user(self, user):
        uid = user.pk
        pref = UserPreference.objects.create(user=user, preferences=default_preferences())
        UserPreferenceAudit.objects.create(
            user=user, action="created", schema_version=SCHEMA_VERSION
        )
        assert UserPreference.objects.filter(pk=pref.pk).exists()
        user.delete()
        assert not UserPreference.objects.filter(pk=pref.pk).exists()
        # Deleting the user removes their audit trail too.
        assert not UserPreferenceAudit.objects.filter(user_id=uid).exists()

    def test_str_does_not_expose_contents(self, user):
        pref = UserPreference.objects.create(
            user=user, preferences={"notes": "very-secret-value"}
        )
        assert "very-secret-value" not in str(pref)
        assert "very-secret-value" not in repr(pref)

    def test_reverse_relation(self, user):
        UserPreference.objects.create(user=user, preferences=default_preferences())
        assert user.preferences.schema_version == SCHEMA_VERSION


class TestUserPreferenceAuditModel:
    def test_audit_timestamps_and_lifecycle(self, user):
        a = UserPreferenceAudit.objects.create(
            user=user, action="patched", schema_version=SCHEMA_VERSION, change_count=3
        )
        assert a.action == "patched"
        assert a.change_count == 3
        assert a.created is not None
        assert a.modified is not None