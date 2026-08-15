# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Replace running-only unique constraint with at-most-one-active constraint.

The old constraint ``unique_agentrun_running_per_type`` only prevented duplicate
RUNNING rows.  The new constraint ``unique_agentrun_active_per_type`` enforces
at most one active (RUNNING **or** PENDING) row per ``run_type``.

Before applying the new constraint we must de-duplicate any existing
PENDING/RUNNING rows that would violate it (e.g. a PENDING and a RUNNING row
for the same run_type left behind by the old schema).  We keep the oldest row
and mark the extras as SKIPPED so the constraint can be created cleanly.
"""

from django.db import migrations, models


def deduplicate_active_runs(apps, schema_editor):
    """Remove duplicate active (RUNNING/PENDING) rows per run_type.

    For each run_type that has more than one active row, keep the oldest
    (lowest pk) and finalize the rest as SKIPPED so they no longer conflict
    with the new partial unique index.
    """
    AgentRun = apps.get_model('crank', 'AgentRun')
    ACTIVE_STATUSES = ['running', 'pending']

    for run_type_qs in AgentRun.objects.filter(status__in=ACTIVE_STATUSES).values('run_type'):
        run_type = run_type_qs['run_type']
        active_rows = list(
            AgentRun.objects.filter(run_type=run_type, status__in=ACTIVE_STATUSES)
            .order_by('id')
        )
        if len(active_rows) <= 1:
            continue
        # Keep the first (oldest); finalize the rest as skipped.
        keep = active_rows[0]
        for extra in active_rows[1:]:
            extra.status = 'skipped'
            extra.save(update_fields=['status'])


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
