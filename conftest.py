# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import os

import pytest


@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    from django.conf import settings

    # Only redirect the test database for the default SQLite suite; a
    # MySQL-variant run (crank/tests/test_mysql_concurrency.py, documented
    # in its docstring) must keep Django's normal test_<NAME> database on
    # the configured MySQL server.
    engine = settings.DATABASES["default"]["ENGINE"]
    if "sqlite" in engine:
        settings.DATABASES["default"]["TEST"]["NAME"] = (
            f"/tmp/sf_gate_{os.getpid()}.sqlite3"
        )
