# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Tests for the read-only rto_reading_report command (issue #537, stage 1)."""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from crank.models.company_profile import CompanyFieldEvidence
from crank.models.organization import Organization
from crank.models.preference import UserPreference, default_preferences

FieldKey = CompanyFieldEvidence.FieldKey
State = CompanyFieldEvidence.State


def evidence(org, value, state=State.ACCEPTED, field_key=FieldKey.RTO_POLICY):
    return CompanyFieldEvidence.objects.create(
        organization=org,
        field_key=field_key,
        value_text=value,
        source_url="https://report.example.test/about",
        source_domain="report.example.test",
        observed_at=timezone.now(),
        validation_version="v1",
        extractor_version="v1",
        state=state,
    )


def prefs_for(username, **work_location):
    user = get_user_model().objects.create_user(username=username)
    document = default_preferences()
    importance = work_location.pop("importance", {})
    document["work_location"].update(work_location)
    document["importance"] = importance
    UserPreference.objects.create(user=user, preferences=document)
    return user


def run():
    out = StringIO()
    call_command("rto_reading_report", stdout=out)
    return out.getvalue()


class RtoReadingReportTests(TestCase):
    def test_empty_database(self):
        text = run()
        assert "distinct_values=0 organizations=0" in text
        assert "values_changed=0" in text
        assert (
            "values_unknown_today=0 values_unknown_proposed=0 "
            "organizations_unknown_today=0 organizations_unknown_proposed=0"
        ) in text
        assert "readers_with_work_location_requirement=0" in text

    def test_reports_each_distinct_accepted_value_once_with_both_readings(self):
        a, b, c = (Organization.objects.create(name=n) for n in ("Alpha", "Beta", "Gamma"))
        evidence(a, "No office required")
        evidence(b, "No office required")
        evidence(c, "Hybrid, 3 days in office")
        evidence(c, "Remote", state=State.CONFLICTED)  # not accepted: ignored
        evidence(c, "Fully remote", field_key=FieldKey.FUNDING_ROUND)  # other field: ignored
        text = run()
        assert "distinct_values=2 organizations=3" in text
        assert 'value="No office required" organizations=2 today=5 proposed=0 CHANGED' in text
        assert 'value="Hybrid, 3 days in office" organizations=1 today=5 proposed=3 CHANGED' in text
        assert "Fully remote" not in text
        assert "values_changed=2" in text

    def test_counts_unknown_values_and_organizations_before_and_after(self):
        a, b, c, d = (Organization.objects.create(name=n) for n in ("Alpha", "Beta", "Gamma", "Delta"))
        evidence(a, "Not remote")  # today 0, proposed unknown
        evidence(b, "Not remote")
        evidence(b, "No remote")  # a second unknown value for one organization
        evidence(b, "Hybrid, 3 days in office")
        evidence(c, "Berlin, Germany")  # unknown both ways
        evidence(d, "On-site, Tuesday-Thursday")  # today unknown, proposed 3
        evidence(d, "In person")  # today unknown, proposed 5
        evidence(d, "RTO")  # today unknown, proposed 5
        text = run()
        # unknown today: Berlin and Delta's three values (4 values, 2 organizations);
        # unknown proposed: both "remote" values and Berlin (3 values, 3 organizations).
        assert (
            "values_unknown_today=4 values_unknown_proposed=3 "
            "organizations_unknown_today=2 organizations_unknown_proposed=3"
        ) in text
    def test_unchanged_value_is_marked_same(self):
        evidence(Organization.objects.create(name="Alpha"), "Remote")
        assert 'value="Remote" organizations=1 today=0 proposed=0 same' in run()
        assert "values_changed=0" in run()

    def test_counts_readers_whose_exclusions_or_matches_differ(self):
        a, b, c = (Organization.objects.create(name=n) for n in ("Alpha", "Beta", "Gamma"))
        evidence(a, "No office required")  # today 5, proposed 0
        evidence(b, "Not remote")  # today 0, proposed unknown
        evidence(c, "Hybrid 3 days")  # 3 and 3
        # hard remote-only: Alpha swaps from excluded to matched and Beta from matched to
        # unverified (excluded): the excluded set changes, one match before and after.
        prefs_for("r1", modes=["remote"], importance={"work_location.modes": 1.0})
        # at most 2 days (always hard): the same swap.
        prefs_for("r2", max_in_office_days=2)
        # at most 5 days: Beta stops matching ("Not remote" is unknown), so both counts change.
        prefs_for("r3", max_in_office_days=5)
        # soft in-office preference: never excluded; Alpha stops matching.
        prefs_for("r4", modes=["in-office"], countries=["United States"])  # other paths are skipped
        prefs_for("r5")  # no work-location requirement: not counted
        text = run()
        assert "readers_with_work_location_requirement=4" in text
        assert "readers_with_different_excluded_set=3" in text
        assert "readers_with_different_matched_count=2" in text

    def test_counts_a_reader_whose_hybrid_requirement_a_one_to_four_day_value_now_meets(self):
        evidence(Organization.objects.create(name="Alpha"), "Hybrid, 2 days in office")  # today 5, proposed 2
        # a hard hybrid mode: the earlier reader read in-office (excluded), the new one hybrid (matched)
        prefs_for("r1", modes=["hybrid"], importance={"work_location.modes": 1.0})
        text = run()
        assert "readers_with_work_location_requirement=1" in text
        assert "readers_with_different_excluded_set=1" in text
        assert "readers_with_different_matched_count=1" in text

    def test_prints_no_user_identifier_or_preference_value(self):
        evidence(Organization.objects.create(name="Alpha"), "Remote")
        user = prefs_for("secret-reader-name", modes=["remote"], max_in_office_days=1)
        text = run()
        assert "secret-reader-name" not in text
        assert str(user.pk) not in text.replace("organizations=1", "").replace("today=0", "").replace(
            "proposed=0", ""
        ).replace("=1", "")
        assert "max_in_office_days" not in text

    def test_reads_only_and_stays_inside_a_fixed_number_of_queries(self):
        org = Organization.objects.create(name="Alpha")
        for i in range(5):
            evidence(org, f"Hybrid {i} days")
            prefs_for(f"reader{i}", max_in_office_days=i)
        before = (
            CompanyFieldEvidence.objects.count(),
            UserPreference.objects.count(),
            Organization.objects.count(),
        )
        with self.assertNumQueries(2):
            run()
        assert before == (
            CompanyFieldEvidence.objects.count(),
            UserPreference.objects.count(),
            Organization.objects.count(),
        )
