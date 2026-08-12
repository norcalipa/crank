# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("crank", "0020_companyprofileobservation")]
    operations = [
        migrations.AddField(
            model_name="sourcecatalog",
            name="last_crawl_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="jobsourcecatalog",
            name="last_crawl_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
