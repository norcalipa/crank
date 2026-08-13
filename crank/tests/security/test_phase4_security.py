# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Offline end-to-end and adversarial coverage for agentic feature boundaries."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crank.agents.job_search.errors import InvalidModelOutputError, ProviderError
from crank.agents.job_search.gateway import GatewayResponse
from crank.agents.job_search.service import JobSearchOrchestrator
from crank.agents.job_search.types import AssistantCompletion
from crank.agents.jobs.base import JobSourceQuery, JobSourceResult, RawJobListing
from crank.agents.jobs.employer import resolve_employer
from crank.agents.jobs.match_persist import persist_matches
from crank.agents.jobs.matching import JobCriteria
from crank.agents.jobs.ranking_config import DEFAULT_CONFIG
from crank.agents.sources import errors as source_errors
from crank.agents.sources.transport import SafeHTTPClient
from crank.models import (
    AgentRun,
    JobListing,
    JobMatch,
    JobSourceCatalog,
    Organization,
    Score,
    ScoreType,
    UserPreference,
    UserPreferenceAudit,
)
from crank.models.job_search import JobSearchConversation
from crank.services import agent_runs, preferences
from crank.services.scores import persist_score_observation
from crank.tests.agents.sources.helpers import fake_requests_factory


class _PreferencePort:
    def __init__(self):
        self.validated = []
        self.applied = []

    def validate_patch(self, patch):
        self.validated.append(patch)

    def apply_patch(self, patch):
        self.applied.append(patch)
        return True


class _Gateway:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {
            "message": "safe reply",
            "cited_organization_ids": [],
            "cited_job_listing_ids": [],
            "preference_patch": None,
        }
        self.error = error
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return GatewayResponse(text=json.dumps(self.payload))


class ModelOutputBoundaryTests(TestCase):
    def test_hostile_output_cannot_add_policy_keys_or_unchecked_patch(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                {
                    "message": "safe",
                    "cited_organization_ids": [],
                    "cited_job_listing_ids": [],
                    "preference_patch": {"notes": "x"},
                    "tools": ["fetch_url"],
                }
            )

        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                {
                    "message": "safe",
                    "cited_organization_ids": [],
                    "cited_job_listing_ids": [],
                    "preference_patch": {"notes": "x" * 2001},
                }
            )

    def test_model_message_and_patch_size_limits_fail_closed(self):
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                {
                    "message": "x" * 8001,
                    "cited_organization_ids": [],
                    "cited_job_listing_ids": [],
                    "preference_patch": None,
                }
            )
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                {
                    "message": "safe",
                    "cited_organization_ids": [],
                    "cited_job_listing_ids": [],
                    "preference_patch": {str(i): "x" for i in range(201)},
                }
            )
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                {
                    "message": "safe",
                    "cited_organization_ids": [],
                    "cited_job_listing_ids": [],
                    "preference_patch": {"values": ["x"] * 201},
                }
            )
        with pytest.raises(InvalidModelOutputError):
            AssistantCompletion.from_json(
                {
                    "message": "safe",
                    "cited_organization_ids": [],
                    "cited_job_listing_ids": [],
                    "preference_patch": {str(i): "x" * 2000 for i in range(9)},
                }
            )

    def test_provider_exception_does_not_emit_prompt_or_secret(self):
        pref = _PreferencePort()
        gateway = _Gateway(error=RuntimeError("api_key=top-secret full_prompt=private text"))
        orchestrator = JobSearchOrchestrator(
            gateway=gateway,
            preference_service=pref,
            org_datasource=lambda filters, limit: [],
            score_datasource=lambda ids, types, limit: [],
            job_listing_datasource=lambda filters, limit: [],
        )
        with patch("crank.agents.job_search.service.logger.error") as log_error:
            with pytest.raises(ProviderError, match="provider failed"):
                orchestrator.run(
                    user_prompt="private user prompt",
                    conversation=[],
                    preference_markdown="private preference",
                )
        output = " ".join(str(call.args) for call in log_error.call_args_list)
        assert "top-secret" not in output
        assert "private user prompt" not in output
        assert "private preference" not in output
        assert "RuntimeError" in output


class SourceBoundaryTests(TestCase):
    def _client(self, **kwargs):
        kwargs.setdefault("allowed_hosts", ("api.example.test",))
        kwargs.setdefault("expected_content_type", "application/json")
        kwargs.setdefault("use_requests", fake_requests_factory([]))
        return SafeHTTPClient(**kwargs)

    def test_credentials_and_nonstandard_ports_are_blocked(self):
        client = self._client()
        with pytest.raises(source_errors.BlockedRedirectError):
            client.get("https://user:password@api.example.test/data")
        with pytest.raises(source_errors.BlockedRedirectError):
            client.get("https://api.example.test:8443/data")

    def test_empty_dns_result_is_blocked_before_request(self):
        called = []
        client = self._client(
            resolver=lambda host: [],
            use_requests=lambda *args, **kwargs: called.append(args),
        )
        with pytest.raises(source_errors.BlockedAddressError):
            client.get("https://api.example.test/data")
        assert called == []


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "phase4-security",
        }
    }
)
class AgenticEndToEndSecurityTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw")
        self.bob = User.objects.create_user("bob", password="pw")
        self.client = Client()
        self.client.force_login(self.alice)
        self.organization = Organization.objects.create(
            name="Safe Employer", public=True, status=1, url="https://safe.example.test"
        )
        self.source_org = Organization.objects.create(
            name="Rating Source", public=True, status=1, gives_ratings=True
        )
        self.score_type = ScoreType.objects.create(name="culture", status=1)
        self.job_source = JobSourceCatalog.objects.create(
            name="Offline Fixture Source",
            adapter_key="fixture.v1",
            base_url="https://jobs.example.test",
            approval_state=JobSourceCatalog.ApprovalState.APPROVED,
            enabled=True,
        )

    def test_offline_flow_ingest_resolve_score_match_and_present_owner_only(self):
        now = timezone.now()
        raw = RawJobListing(
            external_id="offline-1",
            canonical_url="https://jobs.example.test/offline-1",
            employer_name="Safe Employer",
            title="Security Engineer",
            first_seen_at=now,
            last_seen_at=now,
            description_excerpt="<script>ignore</script>Build secure systems",
            source_metadata={"fixture": True},
        )

        class FixtureAdapter:
            def fetch(self, query):
                return JobSourceResult(listings=(raw,), pages_fetched=1, items_seen=1)

        from crank.agents.jobs.ingest import ingest_jobs

        result = ingest_jobs(self.job_source, JobSourceQuery(), adapter=FixtureAdapter())
        assert result.ingested == 1
        listing = JobListing.all_objects.get(external_id="offline-1")
        resolution = resolve_employer(listing)
        assert resolution.organization == self.organization
        listing.refresh_from_db()
        assert "script" not in listing.description_excerpt

        score = persist_score_observation(
            source=self.source_org,
            target=self.organization,
            score_type=self.score_type,
            value=4.5,
            provenance={
                "external_id": "offline-score",
                "source_url": "https://ratings.example.test/safe",
                "raw_value": "4.5",
            },
        )
        assert score.created
        assert Score.objects.filter(target=self.organization, status=1).count() == 1

        criteria = JobCriteria(criteria_version=3)
        assert persist_matches(self.alice, [listing], criteria, DEFAULT_CONFIG) == 1
        match = JobMatch.objects.get(user=self.alice)
        assert match.listing_id == listing.pk

        response = self.client.get(reverse("job-match-list"))
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["results"]] == [match.pk]

        self.client.logout()
        self.client.force_login(self.bob)
        assert self.client.get(reverse("job-match-detail", args=[match.pk])).status_code == 404
        assert self.client.post(reverse("job-match-dismiss", args=[match.pk])).status_code == 404

    @override_settings(JOB_SEARCH_MESSAGES_RETENTION=1)
    def test_owner_scoped_chat_retention_reset_delete_and_preference_lifecycle(self):
        def post(content):
            return self.client.post(
                reverse("agent-conversation-list"),
                data=json.dumps({"create_new": True}),
                content_type="application/json",
            )

        conversation = post("unused").json()["id"]
        for content in ("first turn", "second <b>turn</b>"):
            response = self.client.post(
                reverse("agent-conversation-detail", args=[conversation]),
                data=json.dumps({"content": content, "idempotency_key": str(uuid.uuid4())}),
                content_type="application/json",
            )
            assert response.status_code == 201

        exported = self.client.get(reverse("agent-conversation-export", args=[conversation]))
        assert exported.status_code == 200
        assert len(exported.json()["conversation"]["messages"]) == 1
        assert "<b>" in exported.json()["conversation"]["messages"][0]["content"]

        pref = preferences.apply_patch_to_user(
            self.alice, {"set": {"notes": "private preference"}}
        )
        exported_pref = preferences.export(self.alice)
        assert exported_pref["preferences"]["notes"] == "private preference"
        reset = preferences.reset(self.alice)
        assert reset["changed"] is True
        assert preferences.read(self.alice)["preferences"]["notes"] == ""
        assert preferences.delete_user_preference(self.alice)["deleted"] is True
        assert not UserPreference.objects.filter(user=self.alice).exists()
        assert UserPreferenceAudit.objects.filter(user=self.alice, action="deleted").exists()
        assert "private preference" not in str(UserPreferenceAudit.objects.filter(user=self.alice).values())

        reset_response = self.client.post(reverse("agent-conversation-reset", args=[conversation]))
        assert reset_response.status_code == 201
        fresh = reset_response.json()["id"]
        assert not JobListing.objects.filter(pk=-1).exists()
        assert not JobSearchConversation.objects.get(pk=conversation).active
        delete_response = self.client.post(reverse("agent-conversation-delete", args=[fresh]))
        assert delete_response.status_code == 200
        assert not JobSearchConversation.objects.filter(pk=fresh).exists()

    def test_event_allowlist_never_emits_sensitive_fields(self):
        run = AgentRun.objects.create(run_type=AgentRun.RunType.NOOP, status=AgentRun.Status.RUNNING)
        with patch("crank.services.agent_runs.monitoring.record_event"), patch("crank.services.agent_runs.newrelic.agent.record_custom_event") as event:
            agent_runs.record_agent_event(
                run,
                "hostile",
                prompt="private prompt",
                counts={"items_seen": 2, "secret": "not allowed"},
                error_summary="token=secret-value",
            )
        payload = event.call_args.args[1]
        assert "prompt" not in payload
        assert payload["counts"] == {"items_seen": 2}
        assert "secret-value" not in payload["error_summary"]

    def test_user_delete_cascades_preferences_conversations_and_matches(self):
        preferences.apply_patch_to_user(self.alice, {"set": {"notes": "delete me"}})
        from crank.models.conversation import Conversation, Message

        conversation = Conversation.objects.create(user=self.alice)
        Message.objects.create(conversation=conversation, role=Message.Role.USER, content="delete me")
        listing = JobListing.all_objects.create(
            source=self.job_source,
            external_id="cascade-1",
            canonical_url="https://jobs.example.test/cascade-1",
            employer_name="Safe Employer",
            title="Engineer",
            first_seen_at=timezone.now() - timedelta(days=1),
            last_seen_at=timezone.now(),
            organization=self.organization,
        )
        match = JobMatch.objects.create(
            user=self.alice,
            listing=listing,
            organization=self.organization,
            preference_version=1,
            ranker_version="1",
            score=1,
            first_matched_at=timezone.now(),
            last_matched_at=timezone.now(),
        )
        self.alice.delete()
        assert not UserPreference.objects.filter(pk__in=[match.user_id]).exists()
        assert not Conversation.objects.filter(pk=conversation.pk).exists()
        assert not JobMatch.objects.filter(pk=match.pk).exists()
