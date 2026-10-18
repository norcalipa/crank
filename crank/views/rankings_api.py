# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Public reads behind "Updated results available" on the rankings (issue #485).

Both endpoints read the cache entry the page itself embeds, through the same
function (:func:`crank.views.index.ranking_rows`), so what they answer is
exactly what a fresh page would show. They are account-neutral (the same bytes
for every requester), never write the session, and are never stored by a
browser or proxy, including on their refusals.
"""
from django.http import JsonResponse
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from crank.models.score import ScoreAlgorithm
from crank.views.index import ranking_rows, rankings_revision
from crank.views.job_matches import _poll_after_seconds


def _algorithm_is_active(algorithm_id):
    return ScoreAlgorithm.objects.filter(id=algorithm_id, status=1).exists()


# ``require_GET`` is inside ``cache_control`` so the 405 carries the header too;
# an unknown algorithm is a returned 404 (not a raised one) for the same reason.
@cache_control(no_store=True, private=True)
@require_GET
def rankings_revision_view(request, algorithm_id):
    """The cheap poll: the revision of the rows the page would embed now.

    One query on a cache hit (the algorithm check). ``poll_after_seconds`` is
    ``RESULT_REVISION_POLL_SECONDS`` (the one kill switch shared with the
    job-match panel); 0 answers ``null`` and no client polls.
    """
    if not _algorithm_is_active(algorithm_id):
        return JsonResponse({"error": "not_found"}, status=404)
    return JsonResponse({
        "revision": rankings_revision(ranking_rows(algorithm_id)),
        "poll_after_seconds": _poll_after_seconds(),
    })


@cache_control(no_store=True, private=True)
@require_GET
def rankings_list_view(request, algorithm_id):
    """The rows a reader accepts: exactly what the page embeds, and their revision."""
    if not _algorithm_is_active(algorithm_id):
        return JsonResponse({"error": "not_found"}, status=404)
    rows = ranking_rows(algorithm_id)
    return JsonResponse({"revision": rankings_revision(rows), "organizations": rows})
