# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Replace running-only unique constraint with at-most-one-active constraint.

The old constraint ``unique_agentrun_running_per_type`` only prevented duplicate
RUNNING rows.  The new constraint ``unique_agentrun_active_per_type`` enforces
at most one active (RUNNING **or** PENDING) row per ``run_type``.

Before applying the new constraint we must de-duplicate any existing
PENDING/RUNNING rows that would violate it (e.g. a PENDING and a RUNNING row
for the same run_type left behind by the old schema).  We prefer the RUNNING
row over PENDING (a running run is already executing and should not be
interrupted), then age/timestamp tie-break by created time.  Every rewritten
row is logged so operators can reconcile after migration.
"""

import logging

from django.db import migrations, models

logger = logging.getLogger("crank.migrations.0028")

# Status priority: RUNNING (already executing) is preferred over PENDING.
_STATUS_PRIORITY = {"running": 0, "pending": 1}


def deduplicate_active_runs(apps, schema_editor):
    """Remove duplicate active (RUNNING/PENDING) rows per run_type.

    For each DISTINCT run_type that has more than one active row, keep the
    best candidate (preferring RUNNING over PENDING, then oldest by created
    time, then lowest pk as final tie-break) and finalize the rest as
    SKIPPED.  Every rewritten row is logged so operators can reconcile.
    """
    AgentRun = apps.get_model('crank', 'AgentRun')
    ACTIVE_STATUSES = ['running', 'pending']

    # NIT-2: iterate DISTINCT run types only, avoiding redundant scans.
    distinct_types = list(
        AgentRun.objects
        .filter(status__in=ACTIVE_STATUSES)
        .values_list('run_type', flat=True)
        .distinct()
    )

    rewritten_count = 0
    for run_type in distinct_types:
        active_rows = list(
            AgentRun.objects.filter(run_type=run_type, status__in=ACTIVE_STATUSES)
            .order_by('created', 'id')
        )
        if len(active_rows) <= 1:
            continue

        # MINOR-2: prefer RUNNING over PENDING (then age/timestamp tie-break).
        # Sort by status priority first, then created (oldest), then pk.
        active_rows.sort(key=lambda r: (_STATUS_PRIORITY.get(r.status, 99), r.created or r.id, r.id))

        keep = active_rows[0]
        for extra in active_rows[1:]:
            logger.info(
                "deduplicate_active_runs: run_type=%s rewriting row pk=%s "
                "status=%s created=%s -> SKIPPED (keeping pk=%s status=%s)",
                run_type,
                extra.pk,
                extra.status,
                getattr(extra, 'created', None),
                keep.pk,
                keep.status,
            )
            extra.status = 'skipped'
            extra.save(update_fields=['status'])
            rewritten_count += 1

    if rewritten_count:
        logger.info(
            "deduplicate_active_runs: %d row(s) rewritten to SKIPPED across %d run_type(s)",
            rewritten_count,
            len(distinct_types),
        )


class Migration(migrations.Migration):

    dependencies = [
        ('crank', '0027_jobretrievalops'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='agentrun',
            name='unique_agentrun_running_per_type',
        ),
        migrations.RunPython(
            deduplicate_active_runs,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='agentrun',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status__in', ['running', 'pending'])),
                fields=('run_type',),
                name='unique_agentrun_active_per_type',
            ),
        ),
    ]
