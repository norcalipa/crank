# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Add the first-result marker to JobSearchConversation (issue #482)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crank', '0047_companycorrection'),
    ]

    operations = [
        migrations.AddField(
            model_name='jobsearchconversation',
            name='first_result_at',
            field=models.DateTimeField(
                blank=True,
                help_text='When the first assistant reply carrying result cards committed. Claimed with a conditional update so the first-result telemetry event is emitted exactly once.',
                null=True,
            ),
        ),
    ]
