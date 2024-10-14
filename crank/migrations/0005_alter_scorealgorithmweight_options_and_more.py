# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated by Django 4.2.6 on 2023-11-21 22:36

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crank', '0004_alter_score_options_and_more'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='scorealgorithmweight',
            options={},
        ),
        migrations.AddConstraint(
            model_name='scorealgorithmweight',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 1)), fields=('type', 'algorithm', 'status'), name='unique_type_algorithm_status', violation_error_message='There is already an weight for this type'),
        ),
    ]
