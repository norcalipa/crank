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
