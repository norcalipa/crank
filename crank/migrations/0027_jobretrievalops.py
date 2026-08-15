# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Migration for the JobRetrievalOps proxy model.

The ``JobRetrievalOps`` model is ``managed = False`` — it exists solely as a
proxy for admin registration and never stores data.  We use
``SeparateDatabaseAndState`` so the model is registered in Django's migration
state (satisfying ``makemigrations --check``) without creating a real
database table, which was the original contradiction (a ``CreateModel`` for a
``managed=False`` model creates a physical table that Django then ignores).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crank', '0026_jobsearchmessage_results_json'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='JobRetrievalOps',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                    ],
                    options={
                        'verbose_name': 'Job Retrieval Operations',
                        'verbose_name_plural': 'Job Retrieval Operations',
                        'managed': False,
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
