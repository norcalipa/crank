# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #467 (unify deterministic eligibility, match reasons and result revisions)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crank', '0037_userpreference_revision'),
    ]

    operations = [
        migrations.AddField(
            model_name='jobmatch',
            name='preference_revision',
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='jobmatch',
            name='data_revision',
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='jobmatch',
            name='generated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='jobmatch',
            name='requirements',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='jobmatch',
            name='evidence_ids',
            field=models.JSONField(blank=True, default=list),
        ),
    ]