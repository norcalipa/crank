# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
# Merge migration: join the ``0030_jobsearch_turn_state`` leaf (issue #458)
# with the ``0032_score_tuple_anchor`` leaf (issue #461, merged via #495).
# Both branch off ``0029_merge_20260815_1645``; this node depends on both so
# the migration graph has a single head once the branches are united.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('crank', '0030_jobsearch_turn_state'),
        ('crank', '0032_score_tuple_anchor'),
    ]

    operations = [
    ]
