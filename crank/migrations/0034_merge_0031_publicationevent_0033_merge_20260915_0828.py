# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Merge migration: join the ``0031_publicationevent`` leaf (issue #470)
# with the ``0033_merge_20260915_0828`` leaf (issues #458/#461 via #495).
# This node depends on both so the migration graph has a single deployable
# head once the sibling branches are united.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('crank', '0031_publicationevent'),
        ('crank', '0033_merge_20260915_0828'),
    ]

    operations = [
    ]
