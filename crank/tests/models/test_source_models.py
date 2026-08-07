# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from crank.models.agent_run import AgentRun
from crank.models.organization import Organization
from crank.models.score import ScoreType
from crank.models.source import (
    ApprovalState,
    SourceCatalog,
    SourceCatalogAudit,
    SourceRun,
)


def make_rating_org(name="Ratings Org"):
    org = Organization.objects.create(name=name, gives_ratings=True, status=1)
    return org


def make_source(*, adapter_key="test.adapter.v1", base_url="https://ratings.example.test",
                approval_state=ApprovalState.APPROVED, enabled=True, **kwargs):
    org = kwargs.pop("org", None) or make_rating_org()
    kwargs.setdefault("name", f"{adapter_key} source")
    return SourceCatalog.objects.create(
        organization=org,
        adapter_key=adapter_key,
        base_url=base_url,
        approval_state=approval_state,
        enabled=enabled,
        **kwargs,
    )


class SourceCatalogModelTests(TestCase):
    def test_defaults(self):
        src = make_source(approval_state=ApprovalState.PENDING, enabled=False)
        self.assertEqual(src.approval_state, ApprovalState.PENDING)
        self.assertFalse(src.enabled)
        self.assertEqual(src.timeout_seconds, 30)
        self.assertEqual(src.cadence, "daily")
        self.assertIsNone(src.approved_at)
        self.assertEqual(str(src), src.name)

    def test_source_is_unique_per_organization(self):
        org = make_rating_org()
        make_source(org=org, name="first")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SourceCatalog.objects.create(
                    organization=org, name="second",
                    adapter_key="x", base_url="https://ratings.example.test",
                )

    def test_name_is_unique(self):
        make_source(name="Shared Name")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_source(name="Shared Name")

    def test_clean_rejects_non_allowlisted_base_url(self):
        src = make_source(base_url="https://evil.example.com")
        # full_clean() runs the model's clean(), gated for admin/forms.
        with self.assertRaises(ValidationError):
            src.full_clean()

    def test_clean_rejects_plain_http(self):
        src = make_source(base_url="http://ratings.example.test")
        with self.assertRaises(ValidationError):
            src.full_clean()

    def test_clean_allows_subdomain_of_allowlisted_domain(self):
        src = make_source(base_url="https://api.ratings.example.test")
        src.clean()  # must not raise
        self.assertTrue(src.base_url)

    def test_supports_score_type(self):
        st = ScoreType.objects.create(name="culture")
        src = make_source()
        self.assertFalse(src.supports_score_type("culture"))
        src.supported_score_types.add(st)
        self.assertTrue(src.supports_score_type("culture"))
        self.assertFalse(src.supports_score_type("missing"))

    def test_run_answer_last_success_failure_duration_counts(self):
        src = make_source()
        win = next = timezone.now()
        # A later failure must not erase the earlier success (acceptance).
        ok = SourceRun.objects.create(source=src, status=AgentRun.Status.RUNNING,
                                      started_at=win)
        win += timedelta(seconds=5)
        ok.finalize(AgentRun.Status.SUCCEEDED, counts={"items_new": 3},
                    finished_at=win)
        bad = SourceRun.objects.create(source=src, status=AgentRun.Status.RUNNING,
                                       started_at=win)
        win += timedelta(seconds=2)
        bad.finalize(AgentRun.Status.FAILED, counts={"items_seen": 0},
                     error_summary="boom", finished_at=win)

        self.assertEqual(src.last_success_run(), ok)
        self.assertEqual(src.last_failure_run(), bad)
        self.assertEqual(src.last_success_at, ok.finished_at)
        self.assertEqual(src.last_failure_at, bad.finished_at)
        self.assertEqual(src.last_run(), bad)
        self.assertEqual(src.last_run_duration, timedelta(seconds=2))
        self.assertEqual(src.last_run_counts, {"items_seen": 0})
        self.assertEqual(ok.duration, timedelta(seconds=5))


class SourceRunModelTests(TestCase):
    def test_defaults_and_duration(self):
        src = make_source()
        run = SourceRun.objects.create(source=src, status=AgentRun.Status.RUNNING,
                                       started_at=timezone.now())
        run.finalize(AgentRun.Status.SUCCEEDED, counts={"items_new": 1})
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.Status.SUCCEEDED)
        self.assertIsNotNone(run.finished_at)
        self.assertIsNotNone(run.duration)
        self.assertEqual(run.counts, {"items_new": 1})
        self.assertEqual(str(run), f"{src.name} [succeeded]")

    def test_finalize_rejects_invalid_transition(self):
        src = make_source()
        run = SourceRun.objects.create(source=src, status=AgentRun.Status.SUCCEEDED)
        with self.assertRaises(ValueError):
            run.finalize(AgentRun.Status.RUNNING)

    def test_one_running_run_per_source(self):
        src = make_source()
        SourceRun.objects.create(source=src, status=AgentRun.Status.RUNNING)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SourceRun.objects.create(source=src, status=AgentRun.Status.RUNNING)

    def test_terminal_run_does_not_block_new_running(self):
        src = make_source()
        SourceRun.objects.create(source=src, status=AgentRun.Status.SUCCEEDED)
        second = SourceRun.objects.create(source=src, status=AgentRun.Status.RUNNING)
        self.assertEqual(second.status, AgentRun.Status.RUNNING)

    def test_pending_to_terminal_allowed(self):
        src = make_source()
        run = SourceRun.objects.create(source=src, status=AgentRun.Status.PENDING)
        run.finalize(AgentRun.Status.SKIPPED)
        self.assertEqual(run.status, AgentRun.Status.SKIPPED)


class SourceCatalogAuditModelTests(TestCase):
    def test_record_redacts_secret_like_fields(self):
        src = make_source()
        audit = SourceCatalogAudit.record(
            source=src, user=None,
            action=SourceCatalogAudit.Action.CHANGED,
            changes={"enabled": (False, True), "api_key": ("secret-value", "new")},
        )
        record = audit.changed_fields
        self.assertEqual(record["enabled"], {"from": False, "to": True})
        # Secret-named field is redacted, never stores the credential.
        self.assertEqual(record["api_key"], {"from": "<redacted>", "to": "<redacted>"})
        self.assertNotIn("secret-value", str(record))

    def test_record_bounds_note_and_accepts_none_user(self):
        src = make_source()
        audit = SourceCatalogAudit.record(
            source=src, user=None, action=SourceCatalogAudit.Action.CREATED,
            note="x" * 1000,
        )
        self.assertIsNone(audit.user)
        self.assertLessEqual(len(audit.note), 500)
