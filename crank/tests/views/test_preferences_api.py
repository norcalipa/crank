# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Direct priorities endpoints (issue #480)."""
import copy
import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from crank.agents.job_search.providers import _PreferenceServiceAdapter
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.organization import Organization
from crank.models.preference import UserPreference, UserPreferenceAudit
from crank.services import job_matching
from crank.services import preferences as prefs
from crank.tests.agents.test_golden_conversations import ScriptedGateway, make_orchestrator

pytestmark = pytest.mark.django_db

BASE_DOC = {
    "work_location.modes": ["remote"],
    "work_location.max_in_office_days": 0,
    "compensation.minimum_salary": 150000,
}
EDIT_PATCH = {"set": {
    "work_location.modes": ["hybrid"],
    "work_location.max_in_office_days": 2,
}}


@pytest.fixture
def alice(db):
    return get_user_model().objects.create_user("alice", password="pw")


@pytest.fixture
def bob(db):
    return get_user_model().objects.create_user("bob", password="pw")


@pytest.fixture
def client_a(alice):
    client = Client()
    client.force_login(alice)
    return client


def post(client, name, payload):
    return client.post(
        reverse(name), data=json.dumps(payload), content_type="application/json"
    )


def seed(user):
    return prefs.apply_patch_to_user(user, {"set": BASE_DOC})


class TestAuthz:
    @pytest.mark.parametrize("name,method", [
        ("agent-preference-read", "get"),
        ("agent-preference-propose", "post"),
        ("agent-preference-reset", "post"),
    ])
    def test_anonymous_redirected_to_login(self, name, method):
        response = getattr(Client(), method)(reverse(name))
        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_method_restrictions(self, client_a):
        assert client_a.post(reverse("agent-preference-read")).status_code == 405
        assert client_a.get(reverse("agent-preference-propose")).status_code == 405
        assert client_a.get(reverse("agent-preference-reset")).status_code == 405

    @pytest.mark.parametrize("name", ["agent-preference-propose", "agent-preference-reset"])
    def test_csrf_enforced_on_post(self, alice, name):
        client = Client(enforce_csrf_checks=True)
        client.force_login(alice)
        response = client.post(
            reverse(name), data="{}", content_type="application/json"
        )
        assert response.status_code == 403

    def test_owner_isolation(self, alice, bob, client_a):
        seed(alice)
        client_b = Client()
        client_b.force_login(bob)
        body = client_b.get(reverse("agent-preference-read")).json()
        assert body["exists"] is False
        assert body["preferences"] == prefs.default_preferences()
        proposal = post(client_b, "agent-preference-propose", {"patch": EDIT_PATCH}).json()
        assert proposal["base_revision"] == 0
        assert {c["path"] for c in proposal["changes"]} == set(EDIT_PATCH["set"])
        row = UserPreference.objects.get(user=alice)
        reset = post(client_b, "agent-preference-reset", {"expected_revision": 0}).json()
        assert reset["reset"] is False
        row.refresh_from_db()
        assert row.preferences["compensation"]["minimum_salary"] == 150000


class TestRead:
    def test_no_row_creates_none(self, alice, client_a):
        response = client_a.get(reverse("agent-preference-read"))
        assert response.status_code == 200
        assert response["Cache-Control"] == "private, no-store"
        body = response.json()
        assert body["exists"] is False and body["revision"] == 0
        assert body["chips"] == [] and body["unsupported_criteria"] == []
        assert UserPreference.objects.filter(user=alice).count() == 0

    def test_existing_row(self, alice, client_a):
        applied = seed(alice)
        body = client_a.get(reverse("agent-preference-read")).json()
        assert body["exists"] is True and body["revision"] == applied["revision"]
        chips = {c["path"]: c for c in body["chips"]}
        assert chips["work_location.max_in_office_days"]["hard"] is True
        paths = {f["path"] for f in body["fields"]}
        assert "compensation.minimum_total_compensation" in paths
        assert body["preferences"]["compensation"]["minimum_salary"] == 150000

    def test_importance_not_reported_unsupported(self, alice, client_a):
        prefs.apply_patch_to_user(alice, {"set": {
            "importance": {"compensation.minimum_salary": 1.0},
        }})
        body = client_a.get(reverse("agent-preference-read")).json()
        assert "importance" not in body["unsupported_criteria"]


class TestPropose:
    def snapshot(self, user):
        row = UserPreference.objects.filter(user=user).first()
        return (
            row.revision, row.modified, copy.deepcopy(row.preferences),
            UserPreferenceAudit.objects.filter(user=user).count(),
        )

    def test_success_shape_and_no_write(self, alice, client_a):
        seed(alice)
        before = self.snapshot(alice)
        response = post(client_a, "agent-preference-propose", {"patch": EDIT_PATCH})
        assert response.status_code == 200
        assert response["Cache-Control"] == "private, no-store"
        body = response.json()
        assert set(body) == {
            "id", "scope", "changes", "change_count", "base_revision",
            "unsupported_criteria", "token",
        }
        assert body["scope"] == "account" and body["change_count"] == 2
        assert body["token"] == {
            "patch": EDIT_PATCH, "scope": "account",
            "base_revision": before[0], "origin": "direct",
        }
        assert self.snapshot(alice) == before

    def test_search_scope_proposal(self, alice, client_a):
        seed(alice)
        body = post(
            client_a, "agent-preference-propose", {"patch": EDIT_PATCH, "scope": "search"}
        ).json()
        assert body["token"]["scope"] == "search"

    def test_no_row_proposal_creates_none(self, alice, client_a):
        body = post(client_a, "agent-preference-propose", {"patch": EDIT_PATCH}).json()
        assert body["base_revision"] == 0
        assert UserPreference.objects.filter(user=alice).count() == 0

    def test_invalid_value_returns_field_errors(self, alice, client_a):
        seed(alice)
        before = self.snapshot(alice)
        response = post(client_a, "agent-preference-propose", {"patch": {"set": {
            "work_location.max_in_office_days": 9,
            "compensation.minimum_salary": 90000,
        }}})
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["type"] == "invalid_request"
        assert list(error["field_errors"]) == ["work_location.max_in_office_days"]
        assert self.snapshot(alice) == before

    @pytest.mark.parametrize("payload", [
        {"patch": {"set": {"bogus": 1}}},
        {"patch": {"remove": {"nope": None}}},
        {"patch": {}},
        {"patch": {"set": {"culture": ["a"]}, "extra": 1}},
    ])
    def test_unknown_or_ambiguous_patch_is_400(self, client_a, payload):
        response = post(client_a, "agent-preference-propose", payload)
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request"

    @pytest.mark.parametrize("payload", [
        {}, {"patch": "x"}, {"patch": EDIT_PATCH, "scope": "everywhere"},
    ])
    def test_bad_shape_or_scope_is_400(self, client_a, payload):
        assert post(client_a, "agent-preference-propose", payload).status_code == 400

    def test_malformed_json_is_400(self, client_a):
        response = client_a.post(
            reverse("agent-preference-propose"), data="{", content_type="application/json"
        )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "malformed_json"

    def test_matches_orchestrator_proposal(self, alice, client_a):
        seed(alice)
        gateway = ScriptedGateway({
            "message": "Here is a proposal.",
            "cited_organization_ids": [],
            "cited_job_listing_ids": [],
            "preference_patch": EDIT_PATCH,
        })
        result = make_orchestrator(gateway, _PreferenceServiceAdapter(alice)).run(
            user_prompt="hybrid two days please", conversation=[], preference_markdown="",
        )
        chat = result.preference_proposal
        direct = post(client_a, "agent-preference-propose", {"patch": EDIT_PATCH}).json()
        for key in ("changes", "change_count", "base_revision", "unsupported_criteria", "scope"):
            assert direct[key] == chat[key]
        for key in ("patch", "scope", "base_revision"):
            assert direct["token"][key] == chat["token"][key]
        assert direct["token"]["origin"] == "direct"


class TestReset:
    def test_success_with_undo_round_trip(self, alice, client_a):
        applied = seed(alice)
        before = prefs.read(alice)["preferences"]
        response = post(client_a, "agent-preference-reset", {"expected_revision": applied["revision"]})
        assert response.status_code == 200
        assert response["Cache-Control"] == "private, no-store"
        body = response.json()
        assert body["reset"] is True and body["revision"] == applied["revision"] + 1
        assert {c["path"] for c in body["changes"]} == set(BASE_DOC)
        assert prefs.read(alice)["preferences"] == prefs.default_preferences()
        undone = post(client_a, "agent-preference-undo", {"undo": body["undo"]})
        assert undone.status_code == 200
        assert prefs.read(alice)["preferences"] == before

    def test_stale_409(self, alice, client_a):
        applied = seed(alice)
        response = post(client_a, "agent-preference-reset", {"expected_revision": applied["revision"] - 1})
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["type"] == "preference_stale"
        assert error["current_revision"] == applied["revision"]
        assert prefs.read(alice)["preferences"]["compensation"]["minimum_salary"] == 150000

    def test_replayed_reset_is_stale_not_double_applied(self, alice, client_a):
        applied = seed(alice)
        first = post(client_a, "agent-preference-reset", {"expected_revision": applied["revision"]})
        again = post(client_a, "agent-preference-reset", {"expected_revision": applied["revision"]})
        assert first.status_code == 200 and again.status_code == 409

    def test_noop(self, alice, client_a):
        body = post(client_a, "agent-preference-reset", {"expected_revision": 0}).json()
        assert body["reset"] is False and body["undo"] is None

    @pytest.mark.parametrize("value", [None, True, "1", -1, 1.5])
    def test_malformed_revision_is_400(self, client_a, value):
        response = post(client_a, "agent-preference-reset", {"expected_revision": value})
        assert response.status_code == 400

    def test_missing_revision_is_400(self, client_a):
        assert post(client_a, "agent-preference-reset", {}).status_code == 400

    def test_database_lock_maps_to_409(self, alice, client_a, monkeypatch):
        from django.db import OperationalError

        def boom(*args, **kwargs):
            raise OperationalError("database is locked")

        monkeypatch.setattr(prefs, "reset", boom)
        response = post(client_a, "agent-preference-reset", {"expected_revision": 0})
        assert response.status_code == 409
        assert response.json()["error"]["type"] == "preference_stale"

    def test_other_failure_is_500(self, alice, client_a, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(prefs, "reset", boom)
        response = post(client_a, "agent-preference-reset", {"expected_revision": 0})
        assert response.status_code == 500
        assert response.json()["error"]["type"] == "service_error"
        assert "kaboom" not in response.content.decode()


class TestNoChatDependency:
    @override_settings(INTERACTIVE_AGENT_ENABLED=False, JOB_SEARCH_PROVIDER="orchestrator")
    def test_endpoints_work_with_assistant_disabled(self, alice, client_a):
        assert client_a.get(reverse("agent-preference-read")).status_code == 200
        proposal = post(client_a, "agent-preference-propose", {"patch": EDIT_PATCH}).json()
        applied = post(client_a, "agent-preference-apply", {
            "decision": "apply", "proposal": proposal["token"],
        })
        assert applied.status_code == 200
        reset = post(client_a, "agent-preference-reset", {"expected_revision": applied.json()["revision"]})
        assert reset.status_code == 200 and reset.json()["reset"] is True


class TestApplyUndoIdempotencyAndConcurrency:
    def test_apply_replay_is_stale_and_undo_replay_is_stale(self, alice, client_a):
        seed(alice)
        token = post(client_a, "agent-preference-propose", {"patch": EDIT_PATCH}).json()["token"]
        first = post(client_a, "agent-preference-apply", {"decision": "apply", "proposal": token})
        second = post(client_a, "agent-preference-apply", {"decision": "apply", "proposal": token})
        assert first.status_code == 200 and second.status_code == 409
        undo = first.json()["undo"]
        assert post(client_a, "agent-preference-undo", {"undo": undo}).status_code == 200
        assert post(client_a, "agent-preference-undo", {"undo": undo}).status_code == 409

    def test_two_tabs_second_apply_conflicts_and_keeps_first(self, alice, client_a):
        seed(alice)
        token_a = post(client_a, "agent-preference-propose", {"patch": EDIT_PATCH}).json()["token"]
        token_b = post(client_a, "agent-preference-propose", {"patch": {"set": {"culture": ["x"]}}}).json()["token"]
        assert post(client_a, "agent-preference-apply", {"decision": "apply", "proposal": token_a}).status_code == 200
        conflict = post(client_a, "agent-preference-apply", {"decision": "apply", "proposal": token_b})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["current_revision"] == prefs.read_for_editor(alice)["revision"]
        assert prefs.read(alice)["preferences"]["culture"] == []

    def test_search_only_persists_nothing(self, alice, client_a):
        seed(alice)
        row = UserPreference.objects.get(user=alice)
        before = (row.revision, row.modified, UserPreferenceAudit.objects.count())
        token = post(
            client_a, "agent-preference-propose", {"patch": EDIT_PATCH, "scope": "search"}
        ).json()["token"]
        response = post(client_a, "agent-preference-apply", {"decision": "apply", "proposal": token})
        assert response.status_code == 200
        assert response.json()["applied"] is False and response.json()["scope"] == "search"
        row.refresh_from_db()
        assert (row.revision, row.modified, UserPreferenceAudit.objects.count()) == before

    def test_cross_user_token_cannot_touch_other_account(self, alice, bob, client_a):
        seed(alice)
        token = post(client_a, "agent-preference-propose", {"patch": EDIT_PATCH}).json()["token"]
        client_b = Client()
        client_b.force_login(bob)
        response = post(client_b, "agent-preference-apply", {"decision": "apply", "proposal": token})
        assert response.status_code == 409
        assert prefs.read(alice)["preferences"]["work_location"]["modes"] == ["remote"]


class TestChatAndEditorPathsYieldIdenticalDocuments:
    """AC 1: the editor path and the chat path produce the same outcome."""

    def _listing(self, org, source, title, location):
        now = timezone.now()
        return JobListing.all_objects.create(
            source=source, external_id=title.lower(),
            canonical_url="https://jobs.example.test/" + title.lower(),
            employer_name=org.name, title=title, location_text=location,
            first_seen_at=now - timedelta(days=1), last_seen_at=now,
            status=JobListing.Status.ACTIVE, organization=org,
        )

    def test_identical_proposals_documents_and_match_reasons(self, db):
        users = get_user_model()
        chat_user = users.objects.create_user("chat", password="pw")
        edit_user = users.objects.create_user("edit", password="pw")
        org = Organization.objects.create(name="Acme")
        source = JobSourceCatalog.objects.create(
            name="Synthetic", adapter_key="synthetic.v1", base_url="https://jobs.example.test",
        )
        self._listing(org, source, "Engineer", "Remote")
        for user in (chat_user, edit_user):
            prefs.apply_patch_to_user(user, {"set": BASE_DOC})
        snapshots = {u: UserPreference.objects.get(user=u) for u in (chat_user, edit_user)}
        audits = {u: UserPreferenceAudit.objects.filter(user=u).count() for u in snapshots}

        gateway = ScriptedGateway({
            "message": "Proposal.", "cited_organization_ids": [], "cited_job_listing_ids": [],
            "preference_patch": EDIT_PATCH,
        })
        chat_proposal = make_orchestrator(gateway, _PreferenceServiceAdapter(chat_user)).run(
            user_prompt="hybrid two days", conversation=[], preference_markdown="",
        ).preference_proposal
        editor = Client()
        editor.force_login(edit_user)
        editor_proposal = post(editor, "agent-preference-propose", {"patch": EDIT_PATCH}).json()

        assert chat_proposal["changes"] == editor_proposal["changes"]
        assert {c["path"] for c in editor_proposal["changes"]} == set(EDIT_PATCH["set"])
        assert chat_proposal["base_revision"] == editor_proposal["base_revision"]

        for user, before in snapshots.items():
            row = UserPreference.objects.get(user=user)
            assert (row.preferences, row.revision, row.modified) == (
                before.preferences, before.revision, before.modified,
            )
            assert UserPreferenceAudit.objects.filter(user=user).count() == audits[user]

        chat_client = Client()
        chat_client.force_login(chat_user)
        for client, proposal in ((chat_client, chat_proposal), (editor, editor_proposal)):
            response = post(client, "agent-preference-apply", {
                "decision": "apply", "proposal": proposal["token"],
            })
            assert response.status_code == 200

        chat_row = UserPreference.objects.get(user=chat_user)
        edit_row = UserPreference.objects.get(user=edit_user)
        assert chat_row.revision == edit_row.revision == snapshots[chat_user].revision + 1
        assert json.dumps(chat_row.preferences, sort_keys=True) == json.dumps(
            edit_row.preferences, sort_keys=True
        )
        assert edit_row.preferences["compensation"]["minimum_salary"] == 150000
        assert edit_row.preferences["work_location"]["modes"] == ["hybrid"]
        assert chat_row.preferences_markdown == edit_row.preferences_markdown
        reasons = {
            u: [(m.title, m.reasons) for m in job_matching.match_jobs(u)]
            for u in (chat_user, edit_user)
        }
        assert reasons[chat_user] == reasons[edit_user]
