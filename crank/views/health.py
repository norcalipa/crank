# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Health endpoints used by Kubernetes probes."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from crank.capability import capability_report


@require_GET
def readiness(request):
    """Return ready only when migrations are current and capabilities are valid.

    The Kubernetes readiness probe hits this endpoint. A 200 response means
    the pod can serve traffic; a 503 means it should not.

    Two checks are performed:

    1. **Migration check**: the database has no unapplied migrations.
    2. **Capability check**: every enabled capability has its required
       configuration (provider, model, secret keys). A disabled capability
       never affects readiness. An enabled capability missing required
       config holds readiness false so the pod does not serve traffic
       for a feature it cannot safely back.

    All capability diagnostics are safe booleans and non-secret strings;
    secret values are never included in the response.
    """
    # --- migration check ---
    try:
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        pending_count = len(executor.migration_plan(targets))
    except Exception:
        return JsonResponse(
            {"status": "unavailable", "pending_migrations": None,
             "capabilities": None},
            status=503,
        )

    if pending_count:
        return JsonResponse(
            {"status": "not_ready", "pending_migrations": pending_count,
             "capabilities": capability_report().to_dict()},
            status=503,
        )

    # --- capability check ---
    report = capability_report()
    if not report.all_ok:
        return JsonResponse(
            {"status": "not_ready", "pending_migrations": 0,
             "capabilities": report.to_dict()},
            status=503,
        )

    return JsonResponse(
        {"status": "ready", "pending_migrations": 0,
         "capabilities": report.to_dict()},
    )
