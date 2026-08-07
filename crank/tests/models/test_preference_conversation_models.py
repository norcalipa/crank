# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.test import TestCase
from django.contrib.auth.models import User

from crank.admin import (
    ConversationAdmin,
    MessageAdmin,
    UserPreferenceAdmin,
)
from crank.models.conversation import Conversation, Message
from crank.models.preference import UserPreference, default_preferences


class MockStaffRequest:
    def __init__(self, user):
        self.user = user


class UserPreferenceModelTests(TestCase):
    def test_defaults_valid_under_schema_version_1(self):
        user = User.objects.create_user(username="pref-user", password="pw")
        pref = UserPreference.objects.create(user=user)
        self.assertEqual(pref.schema_version, UserPreference.SCHEMA_VERSION)
        self.assertEqual(pref.schema_version, 1)
        # Defaults must be valid schema-v1 documents.
        self.assertEqual(pref.preferences, default_preferences())
        self.assertIn("required", pref.preferences)
        self.assertIn("optional", pref.preferences)
        self.assertIn("exclusions", pref.preferences)
        self.assertIn("notes", pref.preferences)
        self.assertEqual(pref.preferences_markdown, "")
        self.assertIsNotNone(pref.created)
        self.assertIsNotNone(pref.modified)

    def test_only_one_preference_per_user(self):
        user = User.objects.create_user(username="pref-unique", password="pw")
        UserPreference.objects.create(user=user)
        with self.assertRaises(IntegrityError):
            UserPreference.objects.create(user=user)

    def test_str_does_not_expose_preference_content(self):
        user = User.objects.create_user(username="pref-str", password="pw")
        pref = UserPreference.objects.create(
            user=user,
            preferences={"required": {}, "optional": {"min_salary": "secret"}, "exclusions": [], "notes": "secret"},
            preferences_markdown="secret markdown",
        )
        self.assertNotIn("secret", str(pref))
        self.assertIn(pref.user.username, str(pref))

    def test_cascade_delete_with_user(self):
        user = User.objects.create_user(username="pref-cascade", password="pw")
        pref_id = UserPreference.objects.create(user=user).id
        user.delete()
        self.assertFalse(UserPreference.objects.filter(id=pref_id).exists())


class ConversationModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="conv-user", password="pw")

    def test_conversation_defaults(self):
        conv = Conversation.objects.create(user=self.user)
        self.assertEqual(conv.status, Conversation.Status.ACTIVE)
        self.assertEqual(conv.title, "")
        self.assertIsNone(conv.retention_until)
        self.assertIsNotNone(conv.created)

    def test_messages_have_stable_ordering(self):
        conv = Conversation.objects.create(user=self.user)
        Message.objects.create(conversation=conv, role=Message.Role.USER, content="hello", order=1)
        Message.objects.create(conversation=conv, role=Message.Role.ASSISTANT, content="hi", order=2)
        # Ordering must be stable and independent of creation order.
        m3 = Message.objects.create(conversation=conv, role=Message.Role.USER, content="again", order=0)
        msgs = list(conv.messages.all())
        self.assertEqual([m.order for m in msgs], [0, 1, 2])
        self.assertEqual(msgs[0].id, m3.id)

    def test_duplicate_order_in_same_conversation_rejected(self):
        conv = Conversation.objects.create(user=self.user)
        Message.objects.create(conversation=conv, role=Message.Role.USER, content="a", order=1)
        with self.assertRaises(IntegrityError):
            Message.objects.create(conversation=conv, role=Message.Role.USER, content="b", order=1)

    def test_message_role_default_status(self):
        conv = Conversation.objects.create(user=self.user)
        msg = Message.objects.create(conversation=conv, role=Message.Role.USER, content="hi", order=1)
        self.assertEqual(msg.status, Message.Status.SENT)

    def test_str_does_not_expose_content(self):
        conv = Conversation.objects.create(user=self.user)
        msg = Message.objects.create(
            conversation=conv, role=Message.Role.USER, content="super-secret-content", order=1
        )
        self.assertNotIn("super-secret-content", str(msg))
        self.assertNotIn("super-secret-content", str(conv))

    def test_cascade_delete_with_user(self):
        conv = Conversation.objects.create(user=self.user)
        msg = Message.objects.create(conversation=conv, role=Message.Role.USER, content="x", order=1)
        conv_id, msg_id = conv.id, msg.id
        self.user.delete()
        self.assertFalse(Conversation.objects.filter(id=conv_id).exists())
        self.assertFalse(Message.objects.filter(id=msg_id).exists())

    def test_deleting_conversation_cascades_to_messages(self):
        conv = Conversation.objects.create(user=self.user)
        msg = Message.objects.create(conversation=conv, role=Message.Role.USER, content="x", order=1)
        msg_id = msg.id
        conv.delete()
        self.assertFalse(Message.objects.filter(id=msg_id).exists())

    def test_one_users_queryset_cannot_include_another_users_conversations(self):
        """Regression: ownership filter must never leak across users."""
        other = User.objects.create_user(username="conv-other", password="pw")
        Conversation.objects.create(user=other, title="other's secret")
        own_conv = Conversation.objects.create(user=self.user, title="mine")
        # Filtering by the owning user must only return that user's rows.
        self.assertEqual(list(Conversation.objects.filter(user=self.user)), [own_conv])
        self.assertFalse(
            Conversation.objects.filter(user=self.user, title="other's secret").exists()
        )
        # A plain all() must also expose the owner via the FK correctly.
        for conv in Conversation.objects.all():
            self.assertIsNotNone(conv.user)

    def test_messages_indexed_constraint_names_registered(self):
        """Indexes/constraints defined in Meta exist in the schema."""
        with connection.cursor() as cursor:
            tables = set(
                row[0]
                for row in cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('index','table')"
                )
            )
        self.assertIn("crank_conv_user_modified_idx", tables)
        self.assertIn("crank_conv_user_status_idx", tables)
        self.assertIn("crank_msg_conv_order_idx", tables)
        self.assertIn("crank_userpref_user_idx", tables)


class MigrationStateTests(TestCase):
    def test_latest_migration_applies_and_models_match(self):
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        plan = executor.migration_plan([("crank", "0007_conversation_message_userpreference_and_more")])
        self.assertFalse(plan, "Expected migration 0007 to already be applied")


class AdminAuthorizationTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.staff = User.objects.create_user(
            username="admin-staff", password="pw", is_staff=True
        )
        self.non_staff = User.objects.create_user(username="admin-nonstaff", password="pw")

    def test_sensitive_admin_views_are_staff_only(self):
        pref_admin = UserPreferenceAdmin(UserPreference, self.site)
        conv_admin = ConversationAdmin(Conversation, self.site)
        msg_admin = MessageAdmin(Message, self.site)
        for admin in (pref_admin, conv_admin, msg_admin):
            self.assertTrue(admin.has_view_permission(MockStaffRequest(self.staff)))
            self.assertTrue(admin.has_view_permission(MockStaffRequest(self.staff), obj=None))
            self.assertFalse(admin.has_view_permission(MockStaffRequest(self.non_staff)))
            self.assertTrue(admin.has_module_permission(MockStaffRequest(self.staff)))
            self.assertFalse(admin.has_module_permission(MockStaffRequest(self.non_staff)))

    def test_readonly_timestamps(self):
        pref_admin = UserPreferenceAdmin(UserPreference, self.site)
        self.assertIn("created", pref_admin.readonly_fields)
        self.assertIn("modified", pref_admin.readonly_fields)
        self.assertIn("preferences", pref_admin.readonly_fields)
        conv_admin = ConversationAdmin(Conversation, self.site)
        self.assertIn("created", conv_admin.readonly_fields)
        self.assertIn("modified", conv_admin.readonly_fields)

    def test_message_content_not_in_list_display(self):
        msg_admin = MessageAdmin(Message, self.site)
        self.assertNotIn("content", msg_admin.list_display)