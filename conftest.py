# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import os

import pytest


@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    from django.conf import settings

    settings.DATABASES["default"]["TEST"]["NAME"] = (
        f"/tmp/sf_gate_{os.getpid()}.sqlite3"
    )
