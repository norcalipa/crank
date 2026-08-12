# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Health endpoints used by Kubernetes probes."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def readiness(request):
    """Return ready only when the database has no unapplied migrations."""
    try:
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        pending_count = len(executor.migration_plan(targets))
    except Exception:
        return JsonResponse(
            {"status": "unavailable", "pending_migrations": None}, status=503
        )

    if pending_count:
        return JsonResponse(
            {"status": "not_ready", "pending_migrations": pending_count}, status=503
        )

    return JsonResponse({"status": "ready", "pending_migrations": 0})
