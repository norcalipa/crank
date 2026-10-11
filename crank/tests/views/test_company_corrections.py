# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import json
import time
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

DEFAULT_IGNORABLE_SAMPLES = (
    "\u00ad", "\u034f", "\u061c", "\u115f", "\u1160", "\u17b4", "\u17b5", "\u180b", "\u180d",
    "\u180f", "\u3164", "\ufe00", "\ufe0f", "\uffa0", "\U000e0100", "\U000e01ef", "\U000e0001",
    "\u2060", "\u206a", "\ufff0", "\U0001d173",
)
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

    @override_settings(COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR=1000)
    def test_hidden_characters_get_a_clear_400(self):
        self.login()
        for field, value in (
            ("proposed_value", "Hybrid\u202e3 days\u202c"),
            ("note", "n\u200bote\x07"),
            ("evidence_url", "https://exa\u200bmple.com/p"),
            ("evidence_url", "https://ex\u0430mple.com/p"),
            ("evidence_url", "https://[2606:4700:4700::1111]/x"),
            *[
                (field, f"Re{char}mote")
                for char in DEFAULT_IGNORABLE_SAMPLES
                for field in ("proposed_value", "note")
            ],
            *[("evidence_url", f"https://exa{char}mple.com/p") for char in DEFAULT_IGNORABLE_SAMPLES],
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

    def test_account_cap_does_not_affect_other_accounts(self):
        third = User.objects.create_user(username="u3", password="pw-477-xyz")
        with override_settings(COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR=1):
            self.login()
            self.assertEqual(self.post(self.body(field_key="rto_policy")).status_code, 201)
            limited = self.post(self.body(field_key="locations"))
            self.assertEqual(limited.status_code, 429)
            self.assertTrue(1 <= int(limited["Retry-After"]) <= 3600)
            for user in (self.other, third):
                client = Client()
                client.force_login(user)
                self.assertEqual(self.post(self.body(field_key="locations"), client=client).status_code, 201)

    def test_rejected_attempt_cap_is_per_account(self):
        with override_settings(COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR=2):
            self.login()
            for _ in range(2):
                self.assertEqual(self.post(self.body(proposed_value="Remote-first")).status_code, 400)
            limited = self.post(self.body())
            self.assertEqual(limited.status_code, 429)
            self.assertTrue(1 <= int(limited["Retry-After"]) <= 3600)
            client = Client()
            client.force_login(self.other)
            self.assertEqual(self.post(self.body(), client=client).status_code, 201)

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


ADDRESS_ON = {"ENV": "prod", "CLIENT_IP_SOURCE": "cloudflare"}
PEER = "192.0.2.10"
ADDRESS_X = "198.51.100.7"
ADDRESS_Y = "198.51.100.8"
FIELDS = ("rto_policy", "locations", "funding_round")


def _address_key(address):
    from django.test import RequestFactory

    from crank.client_address import client_address

    with override_settings(**ADDRESS_ON):
        return client_address(RequestFactory().get("/", HTTP_CF_CONNECTING_IP=address)).key


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class CompanyCorrectionsAddressLimitTest(TestCase):
    """The per-address layer of issue #538, and proof that it is off by default."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.users = [
            User.objects.create_user(username=f"a{index}", password="pw-538-xyz")
            for index in range(3)
        ]
        self.clients = []
        for user in self.users:
            client = Client()
            client.force_login(user)
            self.clients.append(client)
        self.org = Organization.objects.create(name="Acme", status=1)
        CompanyFieldEvidence.objects.create(
            organization=self.org,
            field_key="rto_policy",
            value_text="Remote-first",
            source_url="https://acme.example.com/p",
            observed_at=timezone.now(),
            validation_version="v",
            extractor_version="v",
        )

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

    def post(self, index=0, address=ADDRESS_X, data=None, **headers):
        if address is not None:
            headers["HTTP_CF_CONNECTING_IP"] = address
        return self.clients[index].post(
            URL,
            json.dumps(data if data is not None else self.body()),
            content_type="application/json",
            REMOTE_ADDR=PEER,
            **headers,
        )

    def rejected(self, index=0, address=ADDRESS_X, **overrides):
        """A 400: by default the evidence link is not https."""
        overrides = overrides or {"evidence_url": "http://acme.example.com/"}
        return self.post(index, address, self.body(**overrides))

    def counts(self, index=0, address=ADDRESS_X):
        """(account rejected, address rejected, account stored, address stored)."""
        user = self.users[index].pk
        key = _address_key(address)
        return tuple(
            cache.get(name, 0)
            for name in (
                f"company-correction-rejected:{user}",
                f"company-correction-ip-rejected:{key}",
                f"company-correction-rate:{user}",
                f"company-correction-ip-rate:{key}",
            )
        )

    def cache_keys(self):
        return sorted(key.split(":", 2)[2] for key in cache._cache)

    def test_off_by_default_ignores_every_header(self):
        from django.conf import settings

        self.assertEqual(settings.CLIENT_IP_SOURCE, "")
        spoofed = {
            "HTTP_X_FORWARDED_FOR": ADDRESS_X,
            "HTTP_X_REAL_IP": ADDRESS_X,
            "HTTP_TRUE_CLIENT_IP": ADDRESS_X,
            "HTTP_FORWARDED": f"for={ADDRESS_X}",
        }
        with override_settings(
            COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=0,
            COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR=0,
        ):
            for env in ("dev", "staging", "prod"):
                cache.clear()
                CompanyCorrection.objects.all().delete()
                with self.subTest(env=env), override_settings(ENV=env):
                    for index in range(3):
                        self.assertEqual(self.post(index, **spoofed).status_code, 201)
                    self.assertEqual(self.rejected(0).status_code, 400)
                    users = sorted(str(user.pk) for user in self.users)
                    # Today's keys, plus one in-flight count per account. The
                    # rejected window exists only for the account that was
                    # rejected, as before #538.
                    expected = [
                        f"company-correction-rate:{user}{suffix}"
                        for user in users
                        for suffix in ("", ":reset")
                    ]
                    expected += [f"company-correction-rejected:{user}:pending" for user in users]
                    expected += [
                        f"company-correction-rejected:{self.users[0].pk}{suffix}"
                        for suffix in ("", ":reset")
                    ]
                    self.assertEqual(self.cache_keys(), sorted(expected))
                    self.assertEqual(self.counts(0), (1, 0, 1, 0))

    def test_off_keeps_todays_per_account_limits_and_responses(self):
        """With the source unset, production answers exactly as before #538."""
        with override_settings(ENV="prod", COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR=1):
            self.assertEqual(self.post(0).status_code, 201)
            limited = self.post(0, data=self.body(field_key="locations"))
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(
                limited.json(),
                {
                    "error": "You have reached the correction limit. Please try again later.",
                    "limit_scope": "account",
                },
            )
            self.assertTrue(3500 <= int(limited["Retry-After"]) <= 3600)
            self.assertEqual(self.post(1).status_code, 201)
        with override_settings(ENV="prod", COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR=2):
            for _ in range(2):
                self.assertEqual(self.rejected(2).status_code, 400)
            self.assertEqual(self.post(2).status_code, 429)
            self.assertEqual(self.post(1, data=self.body(field_key="locations")).status_code, 201)
        self.assertFalse([key for key in self.cache_keys() if "-ip-" in key])

    def test_setting_outside_production_is_ignored(self):
        with override_settings(
            ENV="staging", CLIENT_IP_SOURCE="cloudflare",
            COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=0,
        ):
            self.assertEqual(self.post(0).status_code, 201)
        self.assertFalse([key for key in self.cache_keys() if "-ip-" in key])

    def test_two_clients_behind_one_peer_do_not_share_a_cap(self):
        limits = override_settings(COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=2)
        with limits, override_settings(**ADDRESS_ON):
            self.assertEqual(self.post(0, ADDRESS_X).status_code, 201)
            self.assertEqual(self.post(1, ADDRESS_X).status_code, 201)
            limited = self.post(2, ADDRESS_X)
            self.assertEqual(limited.status_code, 429)
            payload = limited.json()
            self.assertEqual(payload["limit_scope"], "network")
            self.assertEqual(
                payload["error"],
                "Too many corrections were sent from your network this hour. "
                "Please try again later.",
            )
            self.assertTrue(1 <= int(limited["Retry-After"]) <= 3600)
            # Blocked by the network: the account's own counters hold nothing.
            self.assertEqual(self.counts(2, ADDRESS_X), (0, 0, 0, 2))
            self.assertEqual(CompanyCorrection.objects.filter(requester=self.users[2]).count(), 0)
            # The same account from another address, behind the same peer, stores.
            self.assertEqual(self.post(2, ADDRESS_Y).status_code, 201)
            self.assertEqual(self.counts(2, ADDRESS_Y), (0, 0, 1, 1))

    def test_source_off_accounts_sharing_everything_stay_independent(self):
        with override_settings(COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=2):
            for index in range(3):
                self.assertEqual(self.post(index, ADDRESS_X).status_code, 201)

    def test_account_cap_binds_one_account_across_addresses(self):
        with override_settings(COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR=2, **ADDRESS_ON):
            addresses = (ADDRESS_X, ADDRESS_Y, "2001:db8:5::1")
            for address, field in zip(addresses[:2], FIELDS):
                self.assertEqual(
                    self.post(0, address, self.body(field_key=field)).status_code, 201
                )
            limited = self.post(0, addresses[2], self.body(field_key=FIELDS[2]))
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.json()["limit_scope"], "account")
            self.assertIn("You have reached the correction limit", limited.json()["error"])
            self.assertEqual(self.counts(0, addresses[2]), (0, 0, 2, 0))

    def test_address_rejected_cap_accumulates_across_accounts(self):
        with override_settings(COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR=2, **ADDRESS_ON):
            self.assertEqual(self.rejected(0).status_code, 400)
            self.assertEqual(self.rejected(1).status_code, 400)
            limited = self.post(2)
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.json()["limit_scope"], "network")
            self.assertEqual(self.counts(2), (0, 2, 0, 0))
            self.assertEqual(self.post(2, ADDRESS_Y).status_code, 201)

    def test_address_rejected_cap_is_shared_inside_an_ipv6_64(self):
        with override_settings(COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR=2, **ADDRESS_ON):
            self.assertEqual(self.rejected(0, "2001:db8:1:2::1").status_code, 400)
            self.assertEqual(self.rejected(1, "2001:db8:1:2:aaaa:bbbb:cccc:dddd").status_code, 400)
            limited = self.post(2, "2001:db8:1:2::3")
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.json()["limit_scope"], "network")
            self.assertEqual(self.post(2, "2001:db8:1:3::3").status_code, 201)

    def test_counters_after_each_outcome(self):
        with override_settings(**ADDRESS_ON):
            first = self.body()
            self.assertEqual(self.post(0, data=first).status_code, 201)
            self.assertEqual(self.counts(), (0, 0, 1, 1))
            self.assertEqual(self.post(0, data=self.body(proposed_value="Onsite")).status_code, 409)
            self.assertEqual(self.counts(), (1, 1, 1, 1))
            self.assertEqual(self.post(0, data=self.body(organization_id=999999)).status_code, 404)
            self.assertEqual(self.counts(), (2, 2, 1, 1))
            self.assertEqual(self.rejected(0).status_code, 400)
            self.assertEqual(self.counts(), (3, 3, 1, 1))
            self.assertEqual(self.post(0, data=first).status_code, 200)
            self.assertEqual(self.counts(), (3, 3, 1, 1))
            # A same-key pending replay found inside the store also keeps nothing.
            from crank.views import company_corrections as view

            replay = view.JsonResponse({}, status=200)
            with patch.object(view, "_store", return_value=replay):
                self.assertEqual(self.post(0).status_code, 200)
            self.assertEqual(self.counts(), (3, 3, 1, 1))
            self.assertEqual(self.post(0, data=self.body(note="x" * 9000)).status_code, 413)
            self.assertEqual(self.counts(), (3, 3, 1, 1))

    def test_exception_keeps_nothing(self):
        from crank.views import company_corrections as view

        self.clients[0].raise_request_exception = False
        with override_settings(**ADDRESS_ON):
            with patch.object(view, "resolve_field_evidence", side_effect=RuntimeError("boom")):
                self.assertEqual(self.post(0).status_code, 500)
            self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_refusal_at_each_counter_keeps_nothing(self):
        rows = (
            ("COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR", "account"),
            ("COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR", "network"),
            ("COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR", "account"),
            ("COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR", "network"),
        )
        for setting, scope in rows:
            cache.clear()
            with self.subTest(setting=setting), override_settings(**{setting: 0}, **ADDRESS_ON):
                for response in (self.post(0), self.rejected(0)):
                    self.assertEqual(response.status_code, 429)
                    self.assertEqual(response.json()["limit_scope"], scope)
                    self.assertTrue(1 <= int(response["Retry-After"]) <= 3600)
                self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(CompanyCorrection.objects.count(), 0)

    def test_a_refusal_is_not_counted_as_a_rejected_attempt(self):
        with override_settings(COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=1, **ADDRESS_ON):
            self.assertEqual(self.post(0).status_code, 201)
            for _ in range(5):
                self.assertEqual(self.post(1).status_code, 429)
            self.assertEqual(self.counts(1), (0, 0, 0, 1))

    def test_interleaved_rejected_attempts_cannot_overshoot(self):
        from crank.views import company_corrections as view

        settings_cases = (
            ("COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR", 0, "account"),
            ("COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR", 1, "network"),
        )
        real = view.resolve_field_evidence
        for setting, second, scope in settings_cases:
            cache.clear()
            inner = {}

            def interleave(organization, inner=inner, second=second):
                if not inner:
                    inner["response"] = self.rejected(second, proposed_value="Remote-first")
                return real(organization)

            with self.subTest(setting=setting), override_settings(**{setting: 1}, **ADDRESS_ON):
                with patch.object(view, "resolve_field_evidence", side_effect=interleave):
                    first = self.rejected(0, proposed_value="Remote-first")
                self.assertEqual(first.status_code, 400)
                self.assertEqual(inner["response"].status_code, 429)
                self.assertEqual(inner["response"].json()["limit_scope"], scope)
                self.assertEqual(self.counts(0)[:2], (1, 1))
                self.assertEqual(self.counts(second)[1], 1)

    def test_unidentified_request_keeps_the_per_account_caps(self):
        with override_settings(
            COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR=1,
            COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=0,
            **ADDRESS_ON,
        ):
            with self.assertLogs("crank.client_address", level="WARNING") as logs:
                self.assertEqual(self.post(0, address=None).status_code, 201)
            self.assertEqual(
                [record.getMessage() for record in logs.records],
                ["client address unavailable: source=cloudflare reason=missing"],
            )
            with self.assertNoLogs("crank.client_address", level="WARNING"):
                malformed = self.post(1, address="198.51.100.7, 198.51.100.8")
                self.assertEqual(malformed.status_code, 201)
                limited = self.post(0, address=None, data=self.body(field_key="locations"))
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.json()["limit_scope"], "account")
        self.assertFalse([key for key in self.cache_keys() if "-ip-" in key])

    def test_untrusted_headers_cannot_move_a_client_to_another_bucket(self):
        with override_settings(COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=1, **ADDRESS_ON):
            self.assertEqual(self.post(0).status_code, 201)
            limited = self.post(
                1, ADDRESS_X, HTTP_X_FORWARDED_FOR=ADDRESS_Y, HTTP_X_REAL_IP=ADDRESS_Y,
                HTTP_TRUE_CLIENT_IP=ADDRESS_Y, HTTP_FORWARDED=f"for={ADDRESS_Y}",
            )
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.json()["limit_scope"], "network")

    def test_network_retry_after_follows_the_address_window(self):
        with override_settings(COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=1, **ADDRESS_ON):
            self.assertEqual(self.post(0).status_code, 201)
            key = _address_key(ADDRESS_X)
            cache.set(f"company-correction-ip-rate:{key}:reset", time.time() + 120, 120)
            cache.set(f"company-correction-rate:{self.users[1].pk}:reset", time.time() + 3000, 3000)
            limited = self.post(1)
            self.assertEqual(limited.status_code, 429)
            self.assertTrue(115 <= int(limited["Retry-After"]) <= 121)

    def test_no_address_is_kept_in_the_cache(self):
        addresses = (ADDRESS_X, "2001:db8:1:2::1")
        with override_settings(**ADDRESS_ON):
            for index, address in enumerate(addresses):
                self.assertEqual(self.post(index, address).status_code, 201)
                self.assertEqual(self.rejected(index, address).status_code, 400)
        stored = repr(sorted(cache._cache.items()))
        for needle in (ADDRESS_X, "2001:db8", PEER):
            self.assertNotIn(needle, stored)
        keys = [key for key in self.cache_keys() if "-ip-" in key]
        self.assertEqual(len(keys), 10)
        for key in keys:
            self.assertRegex(
                key,
                r"^company-correction-ip-(rate:[0-9a-f]{32}(:reset)?"
                r"|rejected:[0-9a-f]{32}(:reset|:pending)?)$",
            )

    def pending(self, index=0, address=ADDRESS_X):
        """In-flight holds on the (account, address) rejected allowances."""
        return (
            cache.get(f"company-correction-rejected:{self.users[index].pk}:pending", 0),
            cache.get(f"company-correction-ip-rejected:{_address_key(address)}:pending", 0),
        )

    def test_nothing_stays_in_flight_after_any_outcome(self):
        from crank.views import company_corrections as view

        self.clients[0].raise_request_exception = False
        with override_settings(COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR=2, **ADDRESS_ON):
            self.assertEqual(self.post(0).status_code, 201)
            self.assertEqual(self.rejected(0).status_code, 400)
            with patch.object(view, "resolve_field_evidence", side_effect=RuntimeError("boom")):
                self.assertEqual(self.post(0, data=self.body(field_key="locations")).status_code, 500)
            self.assertEqual(self.post(0, data=self.body(field_key="locations")).status_code, 201)
            self.assertEqual(self.post(0, data=self.body(field_key="funding_round")).status_code, 429)
            self.assertEqual(self.pending(), (0, 0))
            self.assertEqual(self.counts(), (1, 1, 2, 2))

    def test_base_exception_releases_every_allowance(self):
        """A request torn down mid-store (not an ``Exception``) holds nothing."""
        from crank.views import company_corrections as view

        class Stopped(BaseException):
            pass

        with override_settings(
            COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR=1,
            COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=1,
            **ADDRESS_ON,
        ):
            with patch.object(view, "resolve_field_evidence", side_effect=Stopped()):
                with self.assertRaises(Stopped):
                    self.post(0)
            self.assertEqual(self.counts(), (0, 0, 0, 0))
            self.assertEqual(self.pending(), (0, 0))
            # The single stored allowance is still available afterwards.
            self.assertEqual(self.post(0).status_code, 201)

    def test_a_refusal_caused_only_by_requests_in_flight_asks_for_a_short_wait(self):
        from crank.views import company_corrections as view

        cases = (
            ("COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR", 0, "account"),
            ("COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR", 1, "network"),
        )
        real = view.resolve_field_evidence
        for setting, second, scope in cases:
            cache.clear()
            CompanyCorrection.objects.all().delete()
            inner = {}

            def interleave(organization, inner=inner, second=second):
                if not inner:
                    inner["response"] = self.post(second, data=self.body(field_key="locations"))
                return real(organization)

            with self.subTest(setting=setting), override_settings(**{setting: 1}, **ADDRESS_ON):
                with patch.object(view, "resolve_field_evidence", side_effect=interleave):
                    self.assertEqual(self.post(0).status_code, 201)
                refused = inner["response"]
                self.assertEqual(refused.status_code, 429)
                self.assertEqual(refused.json()["limit_scope"], scope)
                # Nothing was counted, so there is no hour to wait out.
                self.assertEqual(refused["Retry-After"], "1")
                self.assertEqual(self.counts(0)[:2], (0, 0))
                self.assertEqual(self.pending(0), (0, 0))
                # The cap was not weakened: once it is really reached the
                # refusal carries the window.
                self.assertEqual(self.rejected(second).status_code, 400)
                limited = self.post(second, data=self.body(field_key="locations"))
                self.assertEqual(limited.status_code, 429)
                self.assertEqual(limited.json()["limit_scope"], scope)
                self.assertTrue(3500 <= int(limited["Retry-After"]) <= 3600)

    def test_an_in_flight_refusal_at_a_stored_cap_keeps_the_window(self):
        """Documented limit: a stored cap is also a cap on concurrent requests."""
        from crank.views import company_corrections as view

        real = view.resolve_field_evidence
        inner = {}

        def interleave(organization):
            if not inner:
                inner["response"] = self.rejected(1)
            return real(organization)

        with override_settings(COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=1, **ADDRESS_ON):
            with patch.object(view, "resolve_field_evidence", side_effect=interleave):
                self.assertEqual(self.rejected(0, proposed_value="Remote-first").status_code, 400)
            refused = inner["response"]
            self.assertEqual(refused.status_code, 429)
            self.assertEqual(refused.json()["limit_scope"], "network")
            self.assertTrue(3500 <= int(refused["Retry-After"]) <= 3600)
            self.assertEqual(self.counts(1), (0, 1, 0, 0))

    def test_counters_default_when_settings_are_absent(self):
        from django.conf import settings

        from crank.views import company_corrections as view

        with self.settings():
            del settings.COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR
            del settings.COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR
            del settings.COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR
            del settings.COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR
            counters = view._counters(7, "k")
        self.assertEqual(
            [(c.kind, c.scope, c.key, c.limit) for c in counters],
            [
                ("rejected", "account", "company-correction-rejected:7", 60),
                ("rejected", "network", "company-correction-ip-rejected:k", 120),
                ("stored", "account", "company-correction-rate:7", 10),
                ("stored", "network", "company-correction-ip-rate:k", 20),
            ],
        )
        self.assertEqual(counters[0].keys[1], "company-correction-rejected:7:reset")
        self.assertEqual(counters[0].pending_key, "company-correction-rejected:7:pending")
        self.assertEqual([c.scope for c in view._counters(7)], ["account", "account"])


class _Clock:
    """Stands in for the ``time`` module wherever the limiter reads the clock."""

    def __init__(self):
        self.now = time.time()

    def time(self):
        return self.now

    def at_minute(self, minute, start):
        self.now = start + minute * 60


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class CompanyCorrectionsRejectedWindowTest(TestCase):
    """The rejected-attempt hour starts at the first rejection, as before #538.

    These sequences give the same answers on ``main``: the window is not
    started by a request that is stored, so a user is refused neither earlier
    nor later than today, whether the client address source is off or on.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username="w0", password="pw-538-xyz")
        self.client = Client()
        self.client.force_login(self.user)
        self.org = Organization.objects.create(name="Acme", status=1)
        self.clock = _Clock()
        self.start = self.clock.now
        from crank.views import company_corrections as view

        # The cache computes expiry in one module and checks it in another.
        for patcher in (
            patch("django.core.cache.backends.base.time", self.clock),
            patch("django.core.cache.backends.locmem.time", self.clock),
            patch.object(view, "time", self.clock),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, field_key="rto_policy", evidence_url="https://acme.example.com/careers"):
        return self.client.post(
            URL,
            json.dumps(
                {
                    "organization_id": self.org.pk,
                    "field_key": field_key,
                    "proposed_value": "Hybrid, 3 days",
                    "evidence_url": evidence_url,
                    "scope": {"level": "company", "value": ""},
                    "note": "",
                    "idempotency_key": str(uuid.uuid4()),
                }
            ),
            content_type="application/json",
            HTTP_CF_CONNECTING_IP=ADDRESS_X,
        )

    def reject(self, minute, count):
        self.clock.at_minute(minute, self.start)
        for _ in range(count):
            self.assertEqual(self.post(evidence_url="http://acme.example.com/").status_code, 400)

    def modes(self):
        for name, overrides in (("off", {"ENV": "prod"}), ("on", ADDRESS_ON)):
            cache.clear()
            CompanyCorrection.objects.all().delete()
            self.clock.at_minute(0, self.start)
            with self.subTest(source=name), override_settings(**overrides):
                yield name

    def test_a_stored_correction_does_not_start_the_rejected_window(self):
        # 201 at 0 min, then 30 rejections at each of 50, 61 and 111 min. The
        # window opened at 50 min has ended by 111 min, so 30 are counted and
        # the next attempt at 112 min is allowed.
        for _ in self.modes():
            self.assertEqual(self.post().status_code, 201)
            self.reject(50, 30)
            self.reject(61, 30)
            self.reject(111, 30)
            self.clock.at_minute(112, self.start)
            self.assertEqual(self.post(field_key="locations").status_code, 201)

    def test_the_rejected_window_runs_an_hour_from_the_first_rejection(self):
        # 201 at 0 min and 60 rejections at 59 min: the 61st attempt at 62 min
        # is refused until an hour after the first rejection, not after the 201.
        for _ in self.modes():
            self.assertEqual(self.post().status_code, 201)
            self.reject(59, 60)
            self.clock.at_minute(62, self.start)
            refused = self.post(field_key="locations")
            self.assertEqual(refused.status_code, 429)
            self.assertEqual(refused.json()["limit_scope"], "account")
            self.assertTrue(56 * 60 <= int(refused["Retry-After"]) <= 57 * 60 + 1)
            self.clock.at_minute(118, self.start)
            self.assertEqual(self.post(field_key="locations").status_code, 429)
            self.clock.at_minute(120, self.start)
            self.assertEqual(self.post(field_key="locations").status_code, 201)


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

    def _run_threads(self, work, count=40):
        import threading

        threads = [threading.Thread(target=work, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    @staticmethod
    def _all_keys(accounts):
        return [
            key
            for counters in accounts
            for row in counters
            for key in (*row.keys, row.pending_key)
        ]

    def _assert_window(self, default_cache, counter):
        for key in counter.keys:
            self.assertTrue(1 <= default_cache.ttl(key) <= 3600, key)

    def test_address_stored_cap_holds_across_accounts_under_concurrency(self):
        from django.core.cache import cache as default_cache

        from crank.views import company_corrections as view

        run = f"test-redis-{uuid.uuid4()}"
        with override_settings(
            COMPANY_CORRECTION_RATE_LIMIT_PER_HOUR=10,
            COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR=20,
        ):
            accounts = [view._counters(f"{run}-{index}", run) for index in range(8)]
        granted = []

        def worker(index):
            counters = accounts[index % 8]
            refused = view._reserve_all(counters)
            if refused is None:
                view._settle(counters, 201)
            granted.append(refused)

        try:
            self._run_threads(worker)
            self.assertEqual(granted.count(None), 20)
            self.assertEqual({r.scope for r in granted if r}, {"network"})
            address_stored = accounts[0][3]
            self.assertEqual(default_cache.get(address_stored.key), 20)
            self._assert_window(default_cache, address_stored)
            self.assertEqual(sum(default_cache.get(c[2].key) for c in accounts), 20)
            for counters in accounts:
                for row in counters[:2]:
                    self.assertIsNone(default_cache.get(row.key))
                    self.assertEqual(default_cache.get(row.pending_key, 0), 0)
        finally:
            default_cache.delete_many(self._all_keys(accounts))

    def test_rejected_caps_hold_under_concurrency(self):
        """Asserts only what holds whichever threads win the race."""
        from django.core.cache import cache as default_cache

        from crank.views import company_corrections as view

        cases = (
            ("COMPANY_CORRECTION_REJECTED_LIMIT_PER_HOUR", 1, "account"),
            ("COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR", 8, "network"),
        )
        for setting, account_count, scope in cases:
            run = f"test-redis-{uuid.uuid4()}"
            with override_settings(**{setting: 10}):
                accounts = [
                    view._counters(f"{run}-{index}", run) for index in range(account_count)
                ]
            outcomes = []

            def worker(index, accounts=accounts, outcomes=outcomes):
                counters = accounts[index % len(accounts)]
                refused = view._reserve_all(counters)
                if refused is None:
                    view._settle(counters, 400)
                outcomes.append((index % len(accounts), refused))

            with self.subTest(setting=setting):
                try:
                    self._run_threads(worker)
                    accepted = [account for account, refused in outcomes if refused is None]
                    # Requests settle while others arrive, so in-flight holds
                    # only ever refuse early: never more than the cap is kept.
                    self.assertTrue(1 <= len(accepted) <= 10, len(accepted))
                    self.assertEqual(
                        {refused.scope for _, refused in outcomes if refused}, {scope}
                    )
                    for index, counters in enumerate(accounts):
                        kept = accepted.count(index)
                        for row in counters:
                            value = default_cache.get(row.key, 0)
                            self.assertTrue(0 <= value <= row.limit, (row.key, value))
                            self.assertEqual(default_cache.get(row.pending_key, 0), 0)
                        # Every counter equals the outcomes kept against it.
                        self.assertEqual(default_cache.get(counters[0].key, 0), kept)
                        self.assertEqual(default_cache.get(counters[2].key, 0), 0)
                        self.assertEqual(default_cache.get(counters[3].key, 0), 0)
                        if kept:
                            self._assert_window(default_cache, counters[0])
                    shared = accounts[0][1]
                    self.assertEqual(default_cache.get(shared.key, 0), len(accepted))
                    self._assert_window(default_cache, shared)
                finally:
                    default_cache.delete_many(self._all_keys(accounts))

    def test_one_real_request_counts_against_its_address(self):
        import random

        from django.core.cache import cache as default_cache

        from crank.views import company_corrections as view

        address = "2001:db8:%x:%x::1" % (random.getrandbits(16), random.getrandbits(16))
        organization = Organization.objects.create(name="Redis Acme", status=1)
        client = Client()
        client.force_login(self.user)
        counters = view._counters(self.user.pk, _address_key(address))
        keys = self._all_keys([counters])
        default_cache.delete_many(keys)
        body = {
            "organization_id": organization.pk,
            "field_key": "rto_policy",
            "proposed_value": "Hybrid, 3 days",
            "evidence_url": "https://acme.example.com/careers",
            "scope": {"level": "company", "value": ""},
            "note": "",
            "idempotency_key": str(uuid.uuid4()),
        }
        try:
            with override_settings(**ADDRESS_ON):
                response = client.post(
                    URL, json.dumps(body), content_type="application/json",
                    HTTP_CF_CONNECTING_IP=address,
                )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(
                [default_cache.get(counter.key) for counter in counters], [None, None, 1, 1]
            )
            for counter in counters[:2]:
                self.assertEqual(default_cache.get(counter.pending_key), 0)
                self.assertTrue(1 <= default_cache.ttl(counter.pending_key) <= 3600)
            for counter in counters[2:]:
                self._assert_window(default_cache, counter)
            self.assertIsNotNone(default_cache.get(counters[3].keys[1]))
            self.assertNotIn("2001", " ".join(keys))
        finally:
            default_cache.delete_many(keys)
