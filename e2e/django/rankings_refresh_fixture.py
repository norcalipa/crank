# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Score changes behind an open rankings page (issue #485c).

Run through ``manage.py shell`` with ``ACTION`` set. Scores go through
``persist_score_observation``, the real write path, so the result caches are
cleared on commit exactly as a published score would clear them.

- ``set``: one seeded company's score becomes ``VALUE`` (``ORG`` is the name).
- ``restore``: every seeded company's score goes back to what ``seed_e2e``
  gave it (a no-op when nothing moved), so the other specs' pinned order holds.
- ``pad``: twelve extra scored companies (``PAD_PREFIX``), so the list has a
  second page; ``unpad`` removes them again and clears the result caches. The
  other specs pin "Showing 1-4 of 4 organizations", so a spec that pads always
  unpads in ``afterEach``.
"""
import os

from django.conf import settings
from django.core.cache import cache

from crank.management.commands.seed_e2e import RATING_SOURCE_ORG_NAME, TARGET_ORGS
from crank.models.organization import Organization
from crank.models.score import Score, ScoreType
from crank.services.scores import affected_cache_keys, persist_score_observation

if getattr(settings, "ENV", "") != "dev":
    raise SystemExit("rankings_refresh_fixture is dev-only")

PAD_PREFIX = "E2E Pad "
PAD_COUNT = 12

action = os.environ["ACTION"]
source = Organization.objects.get(name=RATING_SOURCE_ORG_NAME)
score_type = ScoreType.objects.get(name="Culture")


def put(name, value):
    persist_score_observation(
        source=source,
        target=Organization.objects.get(name=name),
        score_type=score_type,
        value=float(value),
    )


if action == "set":
    put(os.environ["ORG"], os.environ["VALUE"])
elif action == "restore":
    for name, value in TARGET_ORGS:
        put(name, value)
elif action == "pad":
    for index in range(PAD_COUNT):
        name = f"{PAD_PREFIX}{index:02d}"
        Organization.objects.get_or_create(
            name=name,
            defaults={
                "type": Organization.Type.COMPANY,
                "url": "https://e2e.example.test/",
                "gives_ratings": False,
                "public": True,
                "funding_round": Organization.FundingRound.PUBLIC,
                "rto_policy": Organization.RTOPolicy.HYBRID,
            },
        )
        put(name, 1.0 + index / 20)
elif action == "unpad":
    pads = Organization.objects.filter(name__startswith=PAD_PREFIX)
    keys = affected_cache_keys(source.pk, score_type.pk)
    # Scores restrict deleting their target, so they go first.
    Score.objects.filter(target__in=pads).delete()
    pads.delete()
    cache.delete_many(keys)
else:
    raise SystemExit(f"unknown ACTION {action!r}")
print(f"rankings_refresh_fixture {action} ok")
