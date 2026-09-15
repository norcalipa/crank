# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""MySQL-configured settings target for the opt-in concurrency variants.

The default suite runs on SQLite, where ``select_for_update`` is a no-op, so
the lock-based serialization guards (#487) are only truly exercised against a
backend with row locks. Point ``DJANGO_SETTINGS_MODULE`` at this module to run
``crank/tests/security/test_mysql_concurrency_variants.py``:

    export CRANK_MYSQL_TEST=1
    export DJANGO_SETTINGS_MODULE=crank.settings_mysql
    export ENV=dev SECRET_KEY=... REDIS_MASTER_URL=redis://localhost:6379/0
    export CRANK_MYSQL_NAME=crank_test CRANK_MYSQL_USER=... CRANK_MYSQL_PASSWORD=...
    python -m pytest crank/tests/security/test_mysql_concurrency_variants.py -v

Every credential is read from the environment (never committed); defaults
target a disposable local MySQL server. This module is for operator-driven,
backend-specific evidence only — the CI suite stays on SQLite.
"""
import os

from crank.settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("CRANK_MYSQL_NAME", "crank_test"),
        "USER": os.environ.get("CRANK_MYSQL_USER", "root"),
        "PASSWORD": os.environ.get("CRANK_MYSQL_PASSWORD", ""),
        "HOST": os.environ.get("CRANK_MYSQL_HOST", "127.0.0.1"),
        "PORT": os.environ.get("CRANK_MYSQL_PORT", "3306"),
        "TEST": {
            "CHARSET": "utf8mb4",
            "COLLATION": "utf8mb4_general_ci",
        },
    }
}
