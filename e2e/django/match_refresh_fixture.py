# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Stored-match state for e2e/django/match-refresh.spec.ts (issue #473).

Run through ``manage.py shell`` with ``ACTION`` set. It only ever touches a
dedicated throwaway account, so the seeded ``e2e_user`` and the shared company
evidence stay exactly as ``seed_e2e`` left them.

- ``arm``: the account has a published generation whose requirements cite an
  evidence row that no longer exists, which is what a stored match looks like
  after its fact was removed.
- ``pause``: the operator switch for recompute is off.
- ``settle``: the account's matches are recomputed and published again, as
  the pipeline does on its own, so a page still showing the old generation
  asks for a re-check that has nothing left to do.
- ``disarm``: remove the account, the switch and the refresh cooldown.
"""
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache

from crank.models.job_match import JobMatch
from crank.models.monitoring import CapabilitySwitch
from crank.models.preference import UserPreference
from crank.services.match_recompute import RecomputeStatus, recompute_user

USERNAME = "e2e_refresh_user"
GONE_EVIDENCE_ID = 2_000_000_000

if getattr(settings, "ENV", "") != "dev":
    raise SystemExit("match_refresh_fixture is dev-only")

User = get_user_model()
action = os.environ["ACTION"]


def disarm():
    for user in User.objects.filter(username=USERNAME):
        cache.delete(f"job-match-refresh:{user.pk}")
        user.delete()
    CapabilitySwitch.objects.filter(key="match_recompute").delete()


if action == "disarm":
    disarm()
elif action == "pause":
    CapabilitySwitch.objects.update_or_create(
        key="match_recompute", defaults={"enabled": False, "note": "e2e: paused"}
    )
elif action == "settle":
    outcome = recompute_user(User.objects.get(username=USERNAME), reason="e2e", force=True)
    assert outcome.status == RecomputeStatus.PUBLISHED, outcome
elif action == "arm":
    disarm()
    user = User.objects.create_user(USERNAME, password=os.environ["E2E_PASSWORD"])
    UserPreference.objects.create(
        user=user, revision=0, preferences={"work_location": {"modes": ["remote"]}}
    )
    outcome = recompute_user(user, reason="e2e", force=True)
    assert outcome.status == RecomputeStatus.PUBLISHED, outcome
    cited = 0
    for match in JobMatch.objects.filter(user=user):
        requirements = match.requirements or []
        for requirement in requirements:
            if isinstance(requirement, dict) and requirement.get("source_kind") == "evidence":
                requirement["source_id"] = GONE_EVIDENCE_ID
                cited += 1
        match.requirements = requirements
        match.save(update_fields=["requirements"])
    assert cited, "the seed must give the account at least one evidence-backed requirement"
else:
    raise SystemExit(f"unknown ACTION {action!r}")
print(f"match_refresh_fixture {action} ok")
