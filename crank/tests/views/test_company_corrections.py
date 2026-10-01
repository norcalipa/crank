# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from crank.models.company_correction import CompanyCorrection
from crank.models.company_profile import CompanyFieldEvidence
from crank.models.organization import Organization
from crank.models.publication import PublicationEvent

URL = "/api/company-corrections/"


def _no_fetch():
    def boom(*args, **kwargs):
        raise AssertionError("submitting a correction must not fetch")

    return (
        patch("crank.agents.sources.transport.SafeHTTPClient", side_effect=boom),
        patch("urllib.request.urlopen", side_effect=boom),
        patch("crank.agents.jobs.firecrawl.FirecrawlClient", side_effect=boom),
    )


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class CompanyCorrectionsViewTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.user = User.objects.create_user(username="u1", password="pw-477-xyz")
        self.other = User.objects.create_user(username="u2", password="pw-477-xyz")
        self.org = Organization.objects.create(name="Acme", status=1)
        self.evidence = CompanyFieldEvidence.objects.create(
            organization=self.org,
            field_key="rto_policy",
            value_text="Remote-first",
            source_url="https://acme.example.com/p",
            observed_at=timezone.now(),
            validation_version="v",
            extractor_version="v",
        )

    def tearDown(self):
        cache.clear()

    def body(self, **overrides):
        data = {
            "organization_id": self.org.pk,
            "field_key": "rto_policy",
            "proposed_value": "Hybrid, 3 days",
            "evidence_url": "https://acme.example.com/careers",
            "scope": {"level": "company", "value": ""},
            "note": "",
            "idempotency_key": str(uuid.uuid4()),
        }
        data.update(overrides)
        return data

    def post(self, data=None, client=None, raw=None):
        client = client or self.client
        content = raw if raw is not None else json.dumps(data if data is not None else self.body())
        return client.post(URL, content, content_type="application/json")

    def login(self, user=None):
        self.client.force_login(user or self.user)

    def test_requires_auth(self):
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.client.get(URL).status_code, 401)
        self.assertEqual(self.client.get(f"{URL}1/").status_code, 401)

    def test_csrf_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(self.post(client=client).status_code, 403)

    def test_method_not_allowed(self):
        self.login()
        self.assertEqual(self.client.put(URL).status_code, 405)
        self.assertEqual(self.client.post(f"{URL}1/", "{}", content_type="application/json").status_code, 405)

    def test_bad_bodies(self):
        self.login()
        self.assertEqual(self.post(raw="not json").status_code, 400)
        self.assertEqual(self.post(raw="[1]").status_code, 400)
        self.assertEqual(self.post(raw=b"\xff\xfe".decode("latin-1")).status_code, 400)
        big = self.body(note="x" * 9000)
        self.assertEqual(self.post(big).status_code, 413)

    def test_unknown_and_inactive_organization(self):
        self.login()
        self.assertEqual(self.post(self.body(organization_id=999999)).status_code, 404)
        inactive = Organization.objects.create(name="Old", status=0)
        self.assertEqual(self.post(self.body(organization_id=inactive.pk)).status_code, 404)
        response = self.post(self.body(organization_id="1"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("organization_id", response.json()["field_errors"])
        response = self.post(self.body(organization_id=True))
        self.assertIn("organization_id", response.json()["field_errors"])

    def test_field_errors(self):
        self.login()
        response = self.post(
            self.body(
                field_key="bogus",
                proposed_value="  ",
                evidence_url="http://acme.example.com/",
                scope={"level": "role", "value": ""},
                note="n" * 501,
                idempotency_key="nope",
            )
        )
        self.assertEqual(response.status_code, 400)
        errors = response.json()["field_errors"]
        for key in (
            "field_key", "proposed_value", "evidence_url", "scope_value", "note", "idempotency_key",
        ):
            self.assertIn(key, errors)
        self.assertEqual(CompanyCorrection.objects.count(), 0)

    def test_team_scope_is_not_accepted(self):
        self.login()
        response = self.post(self.body(scope={"level": "team", "value": "Platform"}))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Team-specific", response.json()["field_errors"]["scope_level"][0])
        self.assertEqual(CompanyCorrection.objects.count(), 0)

    def test_scope_shapes(self):
        self.login()
        response = self.post(self.body(scope="company", proposed_value=["a"]))
        self.assertEqual(response.status_code, 400)
        errors = response.json()["field_errors"]
        self.assertIn("scope_level", errors)
        self.assertIn("proposed_value", errors)
        response = self.post(self.body(scope={"level": "company", "value": "EMEA"}))
        self.assertIn("scope_value", response.json()["field_errors"])
        response = self.post(self.body(scope={"level": "planet", "value": "x"}))
        self.assertIn("scope_level", response.json()["field_errors"])

    def test_missing_required_fields_use_specific_messages(self):
        self.login()
        response = self.post(self.body(field_key="", proposed_value="", evidence_url=""))
        self.assertEqual(response.status_code, 400)
        errors = response.json()["field_errors"]
        self.assertEqual(errors["field_key"], ["Choose what to correct."])
        self.assertEqual(errors["proposed_value"][0], "Enter the corrected value.")
        self.assertEqual(errors["evidence_url"][0], "Add a public link that starts with https://.")

    def test_hostile_urls_and_values_are_rejected_or_kept_inert(self):
        self.login()
        for url in ("javascript:alert(1)", "https://localhost/x", "https://10.0.0.1/", "https://u:p@a.example.com/"):
            response = self.post(self.body(evidence_url=url))
            self.assertEqual(response.status_code, 400, url)
            self.assertIn("evidence_url", response.json()["field_errors"])
        response = self.post(self.body(proposed_value="<script>alert(1)</script>"))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["proposed_value"], "<script>alert(1)</script>")

    def test_equals_current_rejected_case_and_whitespace_insensitive(self):
        self.login()
        response = self.post(self.body(proposed_value="  REMOTE-first "))
        self.assertEqual(response.status_code, 400)
        self.assertIn("proposed_value", response.json()["field_errors"])

    def test_success_payload_and_no_side_effects(self):
        self.login()
        patches = _no_fetch()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        events = PublicationEvent.objects.count()
        orgs = list(Organization.objects.values_list("pk", "name", "status"))
        evidence = list(CompanyFieldEvidence.objects.values_list("pk", "value_text", "state"))
        response = self.post()
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["status_label"], "Pending review")
        self.assertEqual(data["field_label"], "RTO policy")
        self.assertEqual(data["current_value"], "Remote-first")
        self.assertEqual(data["proposed_value"], "Hybrid, 3 days")
        self.assertEqual(data["organization"], {"id": self.org.pk, "name": "Acme"})
        self.assertEqual(data["scope"], {"level": "company", "value": ""})
        record = CompanyCorrection.objects.get(pk=data["id"])
        self.assertEqual(record.requester, self.user)
        self.assertEqual(record.current_evidence, self.evidence)
        self.assertEqual(PublicationEvent.objects.count(), events)
        self.assertEqual(list(Organization.objects.values_list("pk", "name", "status")), orgs)
        self.assertEqual(list(CompanyFieldEvidence.objects.values_list("pk", "value_text", "state")), evidence)

    def test_missing_current_value_snapshot_is_empty(self):
        self.login()
        response = self.post(self.body(field_key="funding_round", proposed_value="Series B"))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["current_value"], "")
        self.assertIsNone(CompanyCorrection.objects.get().current_evidence)

    def test_client_cannot_set_server_fields(self):
        self.login()
        response = self.post(self.body(current_value="forged", status="accepted", requester=self.other.pk))
        self.assertEqual(response.status_code, 201)
        record = CompanyCorrection.objects.get()
        self.assertEqual(record.current_value, "Remote-first")
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.requester, self.user)

    def test_idempotent_replay_returns_200_and_is_not_rate_counted(self):
        self.login()
        data = self.body()
        first = self.post(data)
        self.assertEqual(first.status_code, 201)
        key = f"company-correction-rate:{self.user.pk}"
        before = cache.get(key)
        replay = self.post(data)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["id"], first.json()["id"])
        self.assertEqual(cache.get(key), before)
        self.assertEqual(CompanyCorrection.objects.count(), 1)

    def test_duplicate_pending_returns_409(self):
        self.login()
        first = self.post()
        second = self.post(self.body(proposed_value="Onsite"))
        self.assertEqual(second.status_code, 409)
        existing = second.json()["existing"]
        self.assertEqual(existing["id"], first.json()["id"])
        self.assertEqual(existing["status_label"], "Pending review")
        other_field = self.post(self.body(field_key="funding_round", proposed_value="Seed"))
        self.assertEqual(other_field.status_code, 201)
        self.client.logout()
        self.login(self.other)
        self.assertEqual(self.post().status_code, 201)

    def test_rate_limit(self):
        self.login()
        with override_settings(COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR=1):
            self.assertEqual(self.post().status_code, 201)
            self.assertEqual(self.post(self.body(field_key="locations")).status_code, 429)

    def test_rejected_attempts_do_not_spend_the_allowance(self):
        self.login()
        with override_settings(COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR=2):
            for _ in range(5):
                self.assertEqual(
                    self.post(self.body(evidence_url="http://acme.example.com/")).status_code, 400
                )
            self.assertEqual(self.post(self.body(organization_id=999999)).status_code, 404)
            self.assertEqual(self.post(self.body(proposed_value="Remote-first")).status_code, 400)
            self.assertEqual(self.post().status_code, 201)
            self.assertEqual(self.post(self.body(proposed_value="x")).status_code, 409)
            self.assertEqual(self.post(self.body(field_key="locations")).status_code, 201)
            self.assertEqual(self.post(self.body(field_key="funding_round")).status_code, 429)

    def test_concurrent_submissions_cannot_overshoot_the_limit(self):
        from crank.views import company_corrections as view

        self.login()
        other = Client()
        other.force_login(self.user)
        inner = {}
        real = view.resolve_field_evidence

        def interleave(organization):
            if not inner:
                inner["response"] = self.post(
                    self.body(field_key="locations", proposed_value="Berlin"), client=other
                )
            return real(organization)

        with override_settings(COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR=1):
            with patch.object(view, "resolve_field_evidence", side_effect=interleave):
                first = self.post()
        statuses = sorted([first.status_code, inner["response"].status_code])
        self.assertEqual(statuses, [201, 429])
        self.assertEqual(CompanyCorrection.objects.count(), 1)
        self.assertEqual(cache.get(f"company-correction-rate:{self.user.pk}"), 1)

    def test_rejected_attempts_have_their_own_larger_cap(self):
        self.login()
        with override_settings(COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR=3):
            self.assertEqual(self.post().status_code, 201)
            for _ in range(2):
                self.assertEqual(self.post(self.body(proposed_value="Onsite")).status_code, 409)
            self.assertEqual(self.post(self.body(organization_id=999999)).status_code, 404)
            limited = self.post(self.body(proposed_value="Onsite"))
            self.assertEqual(limited.status_code, 429)
            self.assertTrue(1 <= int(limited["Retry-After"]) <= 3600)
        self.assertEqual(CompanyCorrection.objects.count(), 1)

    def test_exception_releases_the_slot(self):
        from crank.views import company_corrections as view

        self.login()
        key = f"company-correction-rate:{self.user.pk}"
        self.client.raise_request_exception = False
        with patch.object(view, "resolve_field_evidence", side_effect=RuntimeError("boom")):
            self.assertEqual(self.post().status_code, 500)
        self.assertEqual(cache.get(key), 0)

    def test_release_tolerates_an_expired_counter(self):
        from crank.views import company_corrections as view

        view._release("company-correction-missing-key")

    def test_scoped_suggestion_is_refused_while_a_company_wide_fact_exists(self):
        self.login()
        response = self.post(self.body(scope={"level": "location", "value": "London"}))
        self.assertEqual(response.status_code, 400)
        self.assertIn("company-wide", response.json()["field_errors"]["scope_level"][0])
        self.assertEqual(CompanyCorrection.objects.count(), 0)
        ok = self.post(self.body(field_key="funding_round", proposed_value="Seed",
                                 scope={"level": "location", "value": "London"}))
        self.assertEqual(ok.status_code, 201)

    def test_rate_limit_sends_retry_after_from_the_window(self):
        self.login()
        with override_settings(COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR=1):
            self.assertEqual(self.post().status_code, 201)
            limited = self.post(self.body(field_key="locations"))
            self.assertEqual(limited.status_code, 429)
            wait = int(limited["Retry-After"])
            self.assertTrue(3500 <= wait <= 3600)
            cache.delete(f"company-correction-rate:{self.user.pk}:reset")
            self.assertEqual(self.post(self.body(field_key="locations")).status_code, 429)
            self.assertEqual(self.post(self.body(field_key="locations"))["Retry-After"], "3600")

    def test_rate_counter_recovers_when_incr_fails(self):
        self.login()
        key = f"company-correction-rate:{self.user.pk}"
        cache.set(key, 1, 60)
        with patch.object(cache, "incr", side_effect=ValueError):
            self.assertEqual(self.post().status_code, 201)
        self.assertEqual(cache.get(key), 1)

    def test_integrity_race_returns_winner(self):
        self.login()
        data = self.body()
        winner = CompanyCorrection.objects.create(
            requester=self.user,
            organization=self.org,
            field_key="rto_policy",
            proposed_value="Hybrid, 3 days",
            evidence_url="https://acme.example.com/careers",
            idempotency_key=uuid.UUID(data["idempotency_key"]),
        )
        real_filter = CompanyCorrection.objects.filter
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                return CompanyCorrection.objects.none()
            return real_filter(*args, **kwargs)

        with patch.object(CompanyCorrection.objects, "filter", side_effect=flaky):
            response = self.post(data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], winner.pk)

    def test_same_key_pending_race_returns_winner(self):
        self.login()
        data = self.body()
        winner = CompanyCorrection.objects.create(
            requester=self.user,
            organization=self.org,
            field_key="rto_policy",
            proposed_value="Hybrid, 3 days",
            evidence_url="https://acme.example.com/careers",
            idempotency_key=uuid.UUID(data["idempotency_key"]),
        )
        real_filter = CompanyCorrection.objects.filter
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return CompanyCorrection.objects.none()
            return real_filter(*args, **kwargs)

        with patch.object(CompanyCorrection.objects, "filter", side_effect=flaky):
            response = self.post(data)
        self.assertEqual((response.status_code, response.json()["id"]), (200, winner.pk))

    def test_integrity_error_without_winner_propagates(self):
        self.login()
        with patch.object(CompanyCorrection, "save", side_effect=IntegrityError("boom")):
            with self.assertRaises(IntegrityError):
                self.post()

    def test_list_is_owner_scoped_filtered_and_private(self):
        self.login()
        mine = self.post().json()
        other_org = Organization.objects.create(name="Beta", status=1)
        self.post(self.body(organization_id=other_org.pk))
        self.client.logout()
        self.login(self.other)
        self.post(self.body(proposed_value="Elsewhere"))
        self.client.logout()
        self.login()
        response = self.client.get(URL)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(len(response.json()["corrections"]), 2)
        filtered = self.client.get(f"{URL}?organization={self.org.pk}")
        self.assertEqual([c["id"] for c in filtered.json()["corrections"]], [mine["id"]])
        bad = self.client.get(f"{URL}?organization=abc")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad["Cache-Control"], "private, no-store")

    def test_list_rejects_non_ascii_digit_organization(self):
        self.login()
        for value in ("%C2%B2", "%D9%A1", "abc"):
            response = self.client.get(f"{URL}?organization={value}")
            self.assertEqual(response.status_code, 400)

    def test_hidden_characters_get_a_clear_400(self):
        self.login()
        for field, value in (
            ("proposed_value", "Hybrid\u202e3 days\u202c"),
            ("note", "n\u200bote\x07"),
            ("evidence_url", "https://exa\u200bmple.com/p"),
            ("evidence_url", "https://ex\u0430mple.com/p"),
            ("evidence_url", "https://[2606:4700:4700::1111]/x"),
        ):
            response = self.post(self.body(**{field: value}))
            self.assertEqual(response.status_code, 400, (field, value))
            self.assertIn(field, response.json()["field_errors"])
        self.assertEqual(CompanyCorrection.objects.count(), 0)

    def test_zero_width_variant_of_current_value_is_already_accepted(self):
        self.login()
        response = self.post(self.body(proposed_value="Remote-first"))
        self.assertEqual(response.status_code, 400)
        response = self.post(self.body(proposed_value="\uff32emote-first"))
        self.assertEqual(response.status_code, 400)

    def test_huge_integers_are_a_400_not_a_500(self):
        self.login()
        response = self.post(raw='{"organization_id": ' + "1" * 4400 + "}")
        self.assertEqual(response.status_code, 400)
        response = self.post(self.body(organization_id=2**70))
        self.assertEqual(response.status_code, 400)
        response = self.client.get(f"{URL}?organization={'1' * 4400}")
        self.assertEqual(response.status_code, 400)

    def test_per_address_cap_applies_across_accounts(self):
        third = User.objects.create_user(username="u3", password="pw-477-xyz")
        with override_settings(COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=2):
            for user, field in ((self.user, "rto_policy"), (self.other, "locations")):
                client = Client()
                client.force_login(user)
                self.assertEqual(self.post(self.body(field_key=field), client=client).status_code, 201)
            client = Client()
            client.force_login(third)
            limited = self.post(self.body(field_key="funding_round"), client=client)
            self.assertEqual(limited.status_code, 429)
            self.assertTrue(1 <= int(limited["Retry-After"]) <= 3600)
            self.assertEqual(cache.get(f"company-correction-rate:{third.pk}") or 0, 0)

    def test_per_address_rejection_cap(self):
        with override_settings(COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR=2):
            for user in (self.user, self.other):
                client = Client()
                client.force_login(user)
                self.assertEqual(
                    self.post(self.body(proposed_value="Remote-first"), client=client).status_code, 400
                )
            third = User.objects.create_user(username="u3", password="pw-477-xyz")
            client = Client()
            client.force_login(third)
            self.assertEqual(self.post(self.body(), client=client).status_code, 429)

    def test_detail_is_owner_scoped(self):
        self.login()
        mine = self.post().json()
        detail = self.client.get(f"{URL}{mine['id']}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail["Cache-Control"], "private, no-store")
        self.assertEqual(detail.json()["id"], mine["id"])
        self.client.logout()
        self.login(self.other)
        missing = self.client.get(f"{URL}{mine['id']}/")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing["Cache-Control"], "private, no-store")


class RedisRateLimitTest(TestCase):
    """Locks in the django_redis atomicity the locmem tests only approximate."""

    def setUp(self):
        import redis
        from django.conf import settings

        try:
            client = redis.Redis.from_url(settings.REDIS_MASTER_URL, socket_connect_timeout=1)
            client.ping()
        except Exception:
            self.skipTest("Redis is not available")
        self.user = User.objects.create_user(username="redis-u", password="pw-477-xyz")

    def test_concurrent_reservations_grant_exactly_the_limit(self):
        import threading

        from django.core.cache import cache as default_cache

        from crank.views import company_corrections as view

        base = f"company-correction-rate:test-redis-{uuid.uuid4()}"
        keys = (base, base + ":reset")
        granted = []

        def worker():
            granted.append(view._reserve(keys, 10))

        try:
            threads = [threading.Thread(target=worker) for _ in range(40)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(granted), 10)
            self.assertEqual(default_cache.ttl(keys[0]), 3600)
        finally:
            default_cache.delete_many(list(keys))
