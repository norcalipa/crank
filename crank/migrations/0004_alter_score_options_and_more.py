# Generated by Django 4.2.6 on 2023-11-18 00:43

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('crank', '0003_alter_score_source_alter_score_type_and_more'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='score',
            options={},
        ),
        migrations.AddConstraint(
            model_name='score',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 1)),
                                               fields=('type', 'source', 'target', 'status'),
                                               name='unique_score_type_source_target_status',
                                               violation_error_message='There is already an active score of this type'),
        ),
    ]
