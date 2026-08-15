# Temporary conftest (removed after gate): isolate the pytest test database to a
# PID-unique sqlite file so concurrent external test runs (which share and wipe
# the default test_db.sqlite3) cannot corrupt/lock this run.
import os

import pytest


@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    from django.conf import settings

    settings.DATABASES["default"]["TEST"]["NAME"] = (
        f"/tmp/sf_gate_{os.getpid()}.sqlite3"
    )