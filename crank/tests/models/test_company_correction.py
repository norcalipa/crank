# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import uuid

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from crank.models.company_correction import CompanyCorrection
from crank.models.organization import Organization


class CompanyCorrectionModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="pw-477-xyz")
        self.org = Organization.objects.create(name="Acme", status=1)

    def make(self, **overrides):
        values = {
            "requester": self.user,
            "organization": self.org,
            "field_key": "rto_policy",
            "proposed_value": "  Hybrid   3 days ",
            "evidence_url": "https://Acme.example.com/careers",
            "scope_level": "company",
            "scope_value": "",
            "idempotency_key": uuid.uuid4(),
        }
        values.update(overrides)
        return CompanyCorrection(**values)

    def test_clean_normalizes_whitespace_and_url(self):
        item = self.make()
        item.clean()
        self.assertEqual(item.proposed_value, "Hybrid 3 days")
        self.assertEqual(item.evidence_url, "https://acme.example.com/careers")

    def test_unsafe_evidence_urls_rejected(self):
        for url in (
            "http://acme.example.com/",
            "https://localhost/",
            "https://10.0.0.1/",
            "https://[::1]/",
            "https://user:pass@acme.example.com/",
            "https://acme.example.com/#frag",
            "https://acme.example.com:8443/",
            "https://db.internal/",
            "https://acme.example.com/" + "a" * 750,
            "",
            "javascript:alert(1)",
        ):
            with self.subTest(url=url):
                item = self.make(evidence_url=url)
                with self.assertRaises(ValidationError) as ctx:
                    item.full_clean(exclude=["idempotency_key"])
                self.assertIn("evidence_url", ctx.exception.message_dict)

    def test_scope_rules(self):
        with self.assertRaises(ValidationError) as ctx:
            self.make(scope_level="company", scope_value="EMEA").clean()
        self.assertIn("scope_value", ctx.exception.message_dict)
        with self.assertRaises(ValidationError) as ctx:
            self.make(scope_level="team", scope_value="").clean()
        self.assertIn("scope_value", ctx.exception.message_dict)
        with self.assertRaises(ValidationError) as ctx:
            self.make(scope_level="role", scope_value="x" * 101).clean()
        self.assertIn("scope_value", ctx.exception.message_dict)
        self.make(scope_level="location", scope_value="Germany").clean()

    def test_value_and_note_bounds(self):
        with self.assertRaises(ValidationError) as ctx:
            self.make(proposed_value="   ").clean()
        self.assertIn("proposed_value", ctx.exception.message_dict)
        with self.assertRaises(ValidationError) as ctx:
            self.make(proposed_value="v" * 501, note="n" * 501).clean()
        self.assertIn("proposed_value", ctx.exception.message_dict)
        self.assertIn("note", ctx.exception.message_dict)

    def test_idempotency_key_unique_per_requester(self):
        key = uuid.uuid4()
        first = self.make(idempotency_key=key)
        first.clean()
        first.save()
        other = User.objects.create_user(username="o", password="pw-477-xyz")
        self.make(requester=other, idempotency_key=key).save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make(idempotency_key=key).save()

    def test_str_carries_no_values(self):
        item = self.make(proposed_value="secret-value")
        item.save()
        self.assertNotIn("secret-value", str(item))
        self.assertEqual(str(item), f"{self.org.pk}:rto_policy [pending]")

    def test_hidden_characters_are_rejected_in_every_text_field(self):
        for char in ("\u202e", "\u200b", "\x07", "\ufeff", "\u2028"):
            for name, extra in (
                ("proposed_value", {}),
                ("scope_value", {"scope_level": "role"}),
                ("note", {}),
            ):
                with self.assertRaises(ValidationError, msg=f"{name} {char!r}") as ctx:
                    self.make(**{name: f"ab{char}cd"}, **extra).clean()
                self.assertIn(name, ctx.exception.message_dict)

    def test_note_keeps_newlines_but_nfkc_normalizes(self):
        item = self.make(note="line one\nline two \uff21")
        item.clean()
        self.assertEqual(item.note, "line one\nline two A")

    def test_evidence_url_hostile_hosts(self):
        for url in (
            "https://exa\u200bmple.com/p",
            "https://acme.com\u202e.evil.example/p",
            "https://ex\u0430mple.com/p",
            "https://[2606:4700:4700::1111]/x",
            "https://1.1.1.1/x",
            "https://exa\x07mple.com/p",
        ):
            with self.assertRaises(ValidationError, msg=url) as ctx:
                self.make(evidence_url=url).clean()
            self.assertIn("evidence_url", ctx.exception.message_dict)

    def test_evidence_url_idna_host_is_stored_as_punycode(self):
        item = self.make(evidence_url="https://B\u00fccher.example.com/Path?q=1")
        item.clean()
        self.assertEqual(item.evidence_url, "https://xn--bcher-kva.example.com/Path?q=1")
        item = self.make(evidence_url="https://acme.example.com:443/x")
        item.clean()
        self.assertEqual(item.evidence_url, "https://acme.example.com:443/x")

    def test_unparseable_and_overlong_idna_hosts_are_rejected(self):
        for url in ("https://[::1/x", "https://\u00e9" + "a" * 62 + ".example.com/"):
            with self.assertRaises(ValidationError, msg=url):
                self.make(evidence_url=url).clean()

    def test_evidence_url_length_and_bad_idna(self):
        with self.assertRaises(ValidationError):
            self.make(evidence_url="https://acme.example.com/" + "a" * 760).clean()
        with self.assertRaises(ValidationError):
            self.make(evidence_url="https://" + "a" * 70 + ".example.com/").clean()

    def test_canonical_text_folds_hidden_and_compat_characters(self):
        from crank.models.company_correction import canonical_text

        self.assertEqual(canonical_text("Remote\u200b-first"), canonical_text("remote-first"))
        self.assertEqual(canonical_text("\uff28ybrid  3"), "hybrid 3")
