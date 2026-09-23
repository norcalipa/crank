# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #475 (versioned, CAS-published job-match recomputation)

import django.db.models.deletion
import django_extensions.db.fields
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('crank', '0038_jobmatch_result_revision'),
    ]

    operations = [
        migrations.CreateModel(
            name='MatchResultState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', django_extensions.db.fields.CreationDateTimeField(auto_now_add=True, verbose_name='created')),
                ('modified', django_extensions.db.fields.ModificationDateTimeField(auto_now=True, verbose_name='modified')),
                ('issued_generation', models.PositiveBigIntegerField(default=0)),
                ('current_generation', models.PositiveBigIntegerField(blank=True, null=True)),
                ('preference_revision', models.PositiveBigIntegerField(blank=True, null=True)),
                ('preference_version', models.PositiveIntegerField(blank=True, null=True)),
                ('ranker_version', models.CharField(blank=True, default='', max_length=32)),
                ('data_revision', models.PositiveBigIntegerField(blank=True, null=True)),
                ('generated_at', models.DateTimeField(blank=True, null=True)),
                ('result_count', models.PositiveIntegerField(default=0)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='match_result_state', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='jobmatch',
            name='result_generation',
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='jobmatch',
            index=models.Index(fields=['user', 'result_generation'], name='crank_jobmatch_user_gen_idx'),
        ),
        migrations.AlterField(
            model_name='agentrun',
            name='run_type',
            field=models.CharField(
                choices=[
                    ('noop', 'No-op reference run'),
                    ('gather_scores', 'Score gathering run'),
                    ('job_pipeline', 'Job pipeline run'),
                    ('crawl_schedule', 'Crawl scheduling run'),
                    ('crawl', 'On-demand crawl run'),
                    ('match_recompute', 'Match recompute drain run'),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
    ]
