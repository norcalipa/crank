# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Test-only settings: the dev environment backed by a local MySQL server.

CI runs SQLite, but production runs MySQL, where the partial unique
constraint on Score is not emitted (W036) and ``select_for_update`` cannot
lock absent rows -- exactly the backend on which the score first-write race
must be validated (see ``ScoreTupleAnchor`` and the race tests in
``crank/tests/services/test_score_persistence.py``). This module is never
selected by the ENV chain in ``crank/settings/__init__.py`` and is never
used for deployments; select it explicitly, e.g.:

    SECRET_KEY=test REDIS_MASTER_URL=redis://localhost:6379/0 \
    DB_NAME=crank_test DB_USER=... DB_PASS=... DB_HOST=127.0.0.1 \
    python -m pytest crank/tests/services/test_score_persistence.py \
        --ds crank.settings.mysql_test --create-db -v

Django creates and drops the ``test_<DB_NAME>`` database automatically, so
point it at a disposable MySQL server.
"""
import os

import pymysql

from .base import *  # noqa: F401,F403
from .dev import *  # noqa: F401,F403

# The dev DATABASES block is SQLite; mysqlclient may not be installed, so use
# the PyMySQL shim exactly like the production settings module does.
pymysql.install_as_MySQLdb()

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.environ.get('DB_NAME', 'crank_test'),
        'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
        'PORT': os.environ.get('DB_PORT', '3306'),
        'USER': os.environ.get('DB_USER', 'crank'),
        'PASSWORD': os.environ.get('DB_PASS', ''),
        'TEST': {'CHARSET': 'utf8mb4'},
    }
}
