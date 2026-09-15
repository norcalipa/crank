# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import os

import pytest


@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    from django.conf import settings
    from django.db import connection

    if connection.vendor != "sqlite":
        # MySQL-backed runs (crank.settings.mysql_test) keep the settings
        # module's TEST configuration; only SQLite runs get the isolated
        # per-process scratch database.
        return
    settings.DATABASES["default"]["TEST"]["NAME"] = (
        f"/tmp/sf_gate_{os.getpid()}.sqlite3"
    )
