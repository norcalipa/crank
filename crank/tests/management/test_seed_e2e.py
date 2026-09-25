# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the seed_e2e management command (issue #491)."""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from crank.management.commands.seed_e2e import (
    ACTIVE_LISTING_EXTERNAL_ID,
    ACTIVE_LISTING_URL,
    DEFAULT_E2E_PASSWORD,
    E2E_USERNAME,
    E2E_USERNAME_B,
    FIXTURE_SOURCE_NAME,
    RATING_SOURCE_ORG_NAME,
    TARGET_ORGS,
)
from crank.models.company_profile import CompanyProfileObservation
from crank.models.job import JobListing, JobSourceCatalog
from crank.models.organization import Organization
from crank.models.preference import UserPreference
from crank.models.score import Score, ScoreAlgorithm, ScoreAlgorithmWeight, ScoreType
from crank.settings.base import DEFAULT_ALGORITHM_ID


class SeedE2ECommandTests(TestCase):
    """Dev guard, idempotency, dataset shape, and bounded summary output."""

    def test_refuses_non_dev_environment_prod(self):
        with override_settings(ENV="prod"):
            with self.assertRaises(CommandError):
                call_command("seed_e2e", stdout=StringIO())
        self.assertFalse(Organization.objects.filter(name__startswith="E2E ").exists())

    def test_refuses_non_dev_environment_staging(self):
        with override_settings(ENV="staging"):
            with self.assertRaises(CommandError):
                call_command("seed_e2e", stdout=StringIO())
        self.assertFalse(Organization.objects.filter(name__startswith="E2E ").exists())

    def test_creates_bounded_synthetic_dataset(self):
        out = StringIO()
        call_command("seed_e2e", stdout=out)

        user = get_user_model().objects.get(username=E2E_USERNAME)
        self.assertTrue(user.check_password(DEFAULT_E2E_PASSWORD))
        self.assertFalse(user.is_staff)
        # Non-default preferences so the ranked-match surface computes results.
        preference = UserPreference.objects.get(user=user)
        self.assertEqual(
            preference.preferences["work_location"]["modes"], ["remote"]
        )

        for name, _score in TARGET_ORGS:
            self.assertTrue(Organization.objects.filter(name=name).exists())
        self.assertLessEqual(Organization.objects.filter(name__startswith="E2E ").count(), 10)

        source = JobSourceCatalog.objects.get(name=FIXTURE_SOURCE_NAME)
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.APPROVED)
        self.assertTrue(source.enabled)
        listing = JobListing.all_objects.get(canonical_url=ACTIVE_LISTING_URL)
        self.assertEqual(listing.status, JobListing.Status.ACTIVE)
        self.assertTrue(listing.is_remote)
        self.assertIsNotNone(listing.organization)

    def test_is_idempotent(self):
        call_command("seed_e2e", stdout=StringIO())
        before = {
            "orgs": Organization.objects.filter(name__startswith="E2E ").count(),
            "sources": JobSourceCatalog.objects.count(),
            "listings": JobListing.all_objects.count(),
            "users": get_user_model().objects.filter(username=E2E_USERNAME).count(),
        }
        call_command("seed_e2e", stdout=StringIO())
        after = {
            "orgs": Organization.objects.filter(name__startswith="E2E ").count(),
            "sources": JobSourceCatalog.objects.count(),
            "listings": JobListing.all_objects.count(),
            "users": get_user_model().objects.filter(username=E2E_USERNAME).count(),
        }
        self.assertEqual(before, after)

    # -- MAJOR-5 (adversarial review): reruns must be deterministic ---------

    def _seeded_state(self) -> str:
        """Serialize the full seeded state (ids, fixture fields, and
        created/modified timestamps) so identical-state assertions also
        prove that a re-run performs no writes at all."""
        culture = ScoreType.objects.get(name="Culture")
        querysets = [
            Organization.objects.filter(name__startswith="E2E ").order_by("pk"),
            Score.objects.filter(
                type=culture, source__name=RATING_SOURCE_ORG_NAME
            ).order_by("pk"),
            ScoreAlgorithm.objects.filter(id=DEFAULT_ALGORITHM_ID),
            ScoreAlgorithmWeight.objects.filter(
                algorithm_id=DEFAULT_ALGORITHM_ID, type=culture
            ),
            JobSourceCatalog.objects.filter(name=FIXTURE_SOURCE_NAME),
            JobListing.all_objects.filter(source__name=FIXTURE_SOURCE_NAME).order_by("pk"),
            CompanyProfileObservation.objects.filter(fingerprint="e2e-accepted"),
            get_user_model().objects.filter(username=E2E_USERNAME),
            UserPreference.objects.filter(user__username=E2E_USERNAME),
        ]
        return "\n".join(
            serializers.serialize("json", qs, ensure_ascii=False) for qs in querysets
        )

    def test_rerun_twice_produces_identical_state(self):
        """MAJOR-5: a re-run over already-seeded data is a true no-op — every
        row (including created/modified timestamps and the salted password
        hash) is byte-identical between two consecutive runs."""
        call_command("seed_e2e", stdout=StringIO())
        first = self._seeded_state()
        call_command("seed_e2e", stdout=StringIO())
        second = self._seeded_state()
        self.maxDiff = None
        self.assertEqual(first, second)

    def test_rerun_repairs_deliberately_drifted_fixture_state(self):
        """MAJOR-5: ``get_or_create(defaults=...)`` preserves drift. A re-run
        must reconcile every required fixture field on existing rows (source
        policy, org visibility, score value/thresholds, listing status,
        observation outcome, preferences, account flags) and tear down
        duplicate rows under its own synthetic keys, so the journey data is
        identical even from a drifted database."""
        call_command("seed_e2e", stdout=StringIO())

        culture = ScoreType.objects.get(name="Culture")
        rating_source = Organization.objects.get(name=RATING_SOURCE_ORG_NAME)
        alpha = Organization.objects.get(name="E2E Alpha Corp")

        # Drift the shared algorithm pointer + weight.
        algorithm = ScoreAlgorithm.objects.get(id=DEFAULT_ALGORITHM_ID)
        algorithm.description_content = "drifted.md"
        algorithm.save()
        ScoreAlgorithmWeight.objects.filter(
            algorithm=algorithm, type=culture
        ).update(weight=0.5)

        # Drift the rating source and a target org (wrong visibility/policy).
        rating_source.gives_ratings = False
        rating_source.status = 0
        rating_source.url = "https://drift.example.test/"
        rating_source.save()
        alpha.public = False
        alpha.gives_ratings = True
        alpha.status = 0
        alpha.url = "https://drift.example.test/alpha"
        alpha.save()

        # Drift the Alpha score and add a disabled duplicate row under the
        # same (type, source, target) key.
        Score.objects.filter(
            type=culture, source=rating_source, target=alpha
        ).update(score=1.1, low_threshold=-5.0, high_threshold=1.0)
        Score.objects.create(
            type=culture,
            source=rating_source,
            target=alpha,
            score=0.0,
            low_threshold=0.0,
            high_threshold=1.0,
            status=0,
            deactivate_date=None,
        )

        # Drift the fixture source so the jobs journey loses its data. The
        # drifted values stay on allowlisted hosts / registered adapter keys
        # (model validation refuses anything else) but are wrong for the
        # fixture, so the reconcile must restore the canonical policy.
        source = JobSourceCatalog.objects.get(name=FIXTURE_SOURCE_NAME)
        source.enabled = False
        source.approval_state = JobSourceCatalog.ApprovalState.BLOCKED
        source.adapter_key = "firecrawl-careers"
        source.base_url = "https://remoteok.com/"
        source.catalog_metadata = {"description": "drifted"}
        source.save()

        # Drift the active listing (status/employer/org link) and add a
        # duplicate row under the fixture source.
        listing = JobListing.all_objects.get(canonical_url=ACTIVE_LISTING_URL)
        listing.status = JobListing.Status.EXPIRED
        listing.employer_name = "Drifted Employer"
        listing.organization = None
        listing.is_remote = False
        listing.save()
        JobListing.all_objects.create(
            source=source,
            canonical_url="https://www.usajobs.gov/Job/e2e-seed-drifted-duplicate",
            employer_name="E2E Drifted Duplicate",
            title="E2E Drifted Duplicate Job",
            first_seen_at=listing.first_seen_at,
            last_seen_at=listing.last_seen_at,
            status=JobListing.Status.ACTIVE,
        )

        # Drift the evidence observation and the preference document.
        observation = CompanyProfileObservation.objects.get(fingerprint="e2e-accepted")
        observation.status = CompanyProfileObservation.Status.REJECTED
        observation.save()
        preference = UserPreference.objects.get(user__username=E2E_USERNAME)
        preference.preferences = {}
        preference.save()

        # Drift the account (staff flag + wrong password).
        user = get_user_model().objects.get(username=E2E_USERNAME)
        user.is_staff = True
        user.set_password("drifted-password")
        user.save()

        call_command("seed_e2e", stdout=StringIO())

        # -- Everything is reconciled to canonical fixture state. --
        algorithm.refresh_from_db()
        self.assertEqual(algorithm.description_content, "culture-focused.md")
        self.assertEqual(
            ScoreAlgorithmWeight.objects.get(
                algorithm_id=DEFAULT_ALGORITHM_ID, type=culture
            ).weight,
            1.0,
        )

        rating_source.refresh_from_db()
        self.assertTrue(rating_source.gives_ratings)
        self.assertEqual(rating_source.status, 1)
        self.assertEqual(rating_source.url, "https://e2e.example.test/ratings")
        alpha.refresh_from_db()
        self.assertTrue(alpha.public)
        self.assertFalse(alpha.gives_ratings)
        self.assertEqual(alpha.status, 1)
        self.assertEqual(alpha.url, "https://e2e.example.test/")

        # Exactly one canonical score per target org; drifted duplicate gone.
        for name, score_value in TARGET_ORGS:
            scores = Score.objects.filter(
                type=culture,
                source__name=RATING_SOURCE_ORG_NAME,
                target__name=name,
            )
            self.assertEqual(
                scores.count(),
                1,
                f"expected exactly one Culture score for {name!r}",
            )
            self.assertEqual(scores.get().score, score_value)
            self.assertEqual(scores.get().low_threshold, 0.0)
            self.assertEqual(scores.get().high_threshold, 5.0)

        source.refresh_from_db()
        self.assertTrue(source.enabled)
        self.assertEqual(source.approval_state, JobSourceCatalog.ApprovalState.APPROVED)
        self.assertEqual(source.adapter_key, "usajobs")
        self.assertEqual(source.base_url, "https://data.usajobs.gov/")
        self.assertEqual(
            source.catalog_metadata,
            {
                "description": "E2E fixture source (approved+enabled, dev only).",
                "expiry_days": 30,
                "deletion_days": 90,
            },
        )

        listing.refresh_from_db()
        self.assertEqual(listing.status, JobListing.Status.ACTIVE)
        self.assertEqual(listing.employer_name, "E2E Alpha Corp")
        self.assertEqual(listing.organization_id, alpha.pk)
        self.assertTrue(listing.is_remote)
        # The duplicate listing was torn down: exactly one row remains.
        self.assertEqual(
            JobListing.all_objects.filter(source=source).count(), 1
        )

        observation.refresh_from_db()
        self.assertEqual(observation.status, CompanyProfileObservation.Status.ACCEPTED)

        preference.refresh_from_db()
        self.assertEqual(
            preference.preferences["work_location"]["modes"], ["remote"]
        )

        user.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertTrue(user.check_password(DEFAULT_E2E_PASSWORD))
        self.assertFalse(user.check_password("drifted-password"))

    # -- Canonical-key drift (fix-verification round 2) --------------------

    def test_rerun_repairs_drifted_canonical_url(self):
        """A re-run must reconcile the synthetic-keyed listing row when only
        its canonical_url has drifted. The previous implementation looked the
        row up by URL first while creating with the unchanged synthetic
        external_id, so a URL-only drift made the next run attempt an INSERT
        that died on ``UNIQUE (source, external_id)`` inside the command's
        transaction."""
        call_command("seed_e2e", stdout=StringIO())
        listing = JobListing.all_objects.get(canonical_url=ACTIVE_LISTING_URL)
        listing.canonical_url = "https://www.usajobs.gov/Job/e2e-seed-drifted-url"
        listing.save()

        # Must not raise IntegrityError; the drifted row is repaired in place.
        call_command("seed_e2e", stdout=StringIO())

        self.assertEqual(
            JobListing.all_objects.filter(source__name=FIXTURE_SOURCE_NAME).count(),
            1,
        )
        repaired = JobListing.all_objects.get(
            source__name=FIXTURE_SOURCE_NAME, external_id=ACTIVE_LISTING_EXTERNAL_ID
        )
        self.assertEqual(repaired.pk, listing.pk)
        self.assertEqual(repaired.canonical_url, ACTIVE_LISTING_URL)
        self.assertEqual(repaired.status, JobListing.Status.ACTIVE)
        self.assertEqual(repaired.title, "E2E Seed Software Engineer")

    def test_rerun_repairs_drifted_external_id(self):
        """When the synthetic external_id itself has drifted (no row carries
        it), the drifted leftover under the fixture source is torn down and
        the canonical listing is rebuilt on the synthetic key — the dataset
        ends identical either way, and the URL-keyed leftover can never trip
        ``UNIQUE (source, external_id)`` with an INSERT."""
        call_command("seed_e2e", stdout=StringIO())
        listing = JobListing.all_objects.get(canonical_url=ACTIVE_LISTING_URL)
        listing.external_id = "e2e-seed-drifted-key"
        listing.save()

        call_command("seed_e2e", stdout=StringIO())

        rows = JobListing.all_objects.filter(source__name=FIXTURE_SOURCE_NAME)
        self.assertEqual(rows.count(), 1)
        repaired = rows.get()
        self.assertNotEqual(repaired.pk, listing.pk)
        self.assertEqual(repaired.external_id, ACTIVE_LISTING_EXTERNAL_ID)
        self.assertEqual(repaired.canonical_url, ACTIVE_LISTING_URL)
        self.assertEqual(repaired.status, JobListing.Status.ACTIVE)

    def test_rerun_repairs_keys_split_across_two_drifted_rows(self):
        """The nastiest drift shape: one row holds the canonical URL under a
        foreign external_id while another holds the synthetic external_id
        under a drifted URL. The synthetic-keyed row must be reconciled (and
        the foreign-keyed one torn down) without tripping either UNIQUE
        (source, external_id) or UNIQUE (source, canonical_url)."""
        call_command("seed_e2e", stdout=StringIO())
        source = JobSourceCatalog.objects.get(name=FIXTURE_SOURCE_NAME)
        url_holder = JobListing.all_objects.get(canonical_url=ACTIVE_LISTING_URL)
        url_holder.external_id = "e2e-seed-split-url-holder"
        url_holder.save()
        key_holder = JobListing.all_objects.create(
            source=source,
            canonical_url="https://www.usajobs.gov/Job/e2e-seed-split-drifted",
            external_id=ACTIVE_LISTING_EXTERNAL_ID,
            employer_name="E2E Split Key Holder",
            title="E2E Split Key Holder",
            first_seen_at=url_holder.first_seen_at,
            last_seen_at=url_holder.last_seen_at,
            status=JobListing.Status.ACTIVE,
        )

        call_command("seed_e2e", stdout=StringIO())

        rows = JobListing.all_objects.filter(source=source)
        self.assertEqual(rows.count(), 1)
        repaired = rows.get()
        self.assertEqual(repaired.pk, key_holder.pk)
        self.assertEqual(repaired.external_id, ACTIVE_LISTING_EXTERNAL_ID)
        self.assertEqual(repaired.canonical_url, ACTIVE_LISTING_URL)
        self.assertEqual(repaired.title, "E2E Seed Software Engineer")
        self.assertEqual(repaired.status, JobListing.Status.ACTIVE)

    def test_password_override_is_applied_on_rerun(self):
        call_command("seed_e2e", stdout=StringIO())
        call_command("seed_e2e", "--password", "rotated-throwaway", stdout=StringIO())
        user = get_user_model().objects.get(username=E2E_USERNAME)
        self.assertTrue(user.check_password("rotated-throwaway"))
        self.assertFalse(user.check_password(DEFAULT_E2E_PASSWORD))

    def test_second_account_is_seeded_with_own_preferences(self):
        call_command("seed_e2e", stdout=StringIO())
        user_model = get_user_model()
        first = user_model.objects.get(username=E2E_USERNAME)
        second = user_model.objects.get(username=E2E_USERNAME_B)
        self.assertNotEqual(first.pk, second.pk)
        self.assertTrue(second.check_password(DEFAULT_E2E_PASSWORD))
        self.assertEqual(second.email, f"{E2E_USERNAME_B}@example.test")
        self.assertTrue(UserPreference.objects.filter(user=second).exists())

    def test_summary_is_bounded_and_labelled(self):
        out = StringIO()
        call_command("seed_e2e", stdout=out)
        text = out.getvalue()
        self.assertIn("seed_e2e ready", text)
        self.assertIn("users=2", text)
        # Bounded summary: a single short line, never secrets or tracebacks.
        self.assertNotIn(DEFAULT_E2E_PASSWORD, text)
        summary_lines = [line for line in text.splitlines() if "seed_e2e ready" in line]
        self.assertEqual(len(summary_lines), 1)
        self.assertLess(len(summary_lines[0]), 400)
