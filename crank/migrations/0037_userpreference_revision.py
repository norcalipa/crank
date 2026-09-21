# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #466 (preference propose/apply/revision/undo services)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crank', '0036_company_field_evidence'),
    ]

    operations = [
        migrations.AddField(
            model_name='userpreference',
            name='revision',
            field=models.PositiveBigIntegerField(
                default=0,
                help_text='Monotonic document revision; advances by one per committed change.',
                verbose_name='revision',
            ),
        ),
    ]
