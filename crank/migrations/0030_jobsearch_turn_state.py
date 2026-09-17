# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Create the ``JobSearchTurn`` anchor table (issue #458).

The anchor row owns the per-turn delivery state machine (claim lease,
attempt count, failure code) and carries an **unconditional** unique
constraint on ``(conversation, turn_key)``, so it exists on MySQL where the
message-level *partial* constraint (``unique_jobsearch_message_idempotency``,
W036) is never emitted. ``get_or_create`` + ``select_for_update`` on this row
serialize concurrent same-key submissions on every supported backend, making
the anchor — not the skipped partial index — the production apply-once guard.

Additive only: no existing table is altered and no data is backfilled. Legacy
user rows have no anchor row and keep deriving their state at read time.
"""
import django.db.models.deletion
import django_extensions.db.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crank', '0029_merge_20260815_1645'),
    ]

    operations = [
        migrations.CreateModel(
            name='JobSearchTurn',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', django_extensions.db.fields.CreationDateTimeField(auto_now_add=True, verbose_name='created')),
                ('modified', django_extensions.db.fields.ModificationDateTimeField(auto_now=True, verbose_name='modified')),
                ('turn_key', models.CharField(db_index=True, help_text="The user message's idempotency key; identifies the turn.", max_length=64)),
                ('delivery_state', models.CharField(choices=[('pending', 'Pending'), ('completed', 'Completed'), ('failed', 'Failed')], db_index=True, default='pending', help_text='Claim state of the turn: pending (a worker owns the claim), completed (assistant reply persisted), or failed (retryable while attempts remain).', max_length=16)),
                ('failure_code', models.CharField(blank=True, choices=[('assistant_unavailable', 'Assistant unavailable'), ('provider_timeout', 'Provider timeout'), ('cost_limit', 'Cost limit'), ('invalid_output', 'Invalid output'), ('service_error', 'Service error'), ('unexpected_error', 'Unexpected error'), ('worker_interrupted', 'Worker interrupted'), ('conversation_gone', 'Conversation gone')], default='', help_text="Stable failure code recorded when the turn ends failed; matches the typed error envelope's ``error.type`` (plus the two recovery-only codes).", max_length=32)),
                ('attempt_count', models.PositiveIntegerField(db_index=True, default=0, help_text='Provider executions started for this turn; bounded by JOB_SEARCH_TURN_MAX_ATTEMPTS so poisoned turns cannot loop.')),
                ('lease_expires_at', models.DateTimeField(blank=True, help_text='When the current pending claim may be taken over or reaped; set just above the longest legitimate provider run.', null=True)),
                ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='turns', to='crank.jobsearchconversation')),
            ],
            options={
                'ordering': ['created', 'id'],
                'constraints': [models.UniqueConstraint(fields=('conversation', 'turn_key'), name='unique_jobsearch_turn_per_conversation')],
            },
        ),
    ]
