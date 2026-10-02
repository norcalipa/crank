# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Generated for issue #474 (validated company evidence semantics)

from django.db import migrations, models


class Migration(migrations.Migration):
    # Choices-only AlterField (adds the `pending` and `rejected` claim states).
    # Django treats `choices` as a non-DB attribute, so this emits no SQL on
    # MySQL or SQLite and is safe to rerun; there is nothing to recover.

    dependencies = [
        ("crank", "0045_jobsourcecatalog_consecutive_failures"),
    ]

    operations = [
        migrations.AlterField(
            model_name="companyfieldevidence",
            name="state",
            field=models.CharField(
                choices=[
                    ("accepted", "Accepted"),
                    ("superseded", "Superseded"),
                    ("conflicted", "Conflicted"),
                    ("pending", "Pending review"),
                    ("rejected", "Rejected"),
                ],
                db_index=True,
                default="accepted",
                max_length=16,
            ),
        ),
    ]
