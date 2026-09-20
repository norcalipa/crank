# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import multiprocessing
from pathlib import Path
from django.core.cache.backends.redis import RedisCache
import os


BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEBUG = True
ENV = "dev"
SECRET_KEY = os.environ.get("SECRET_KEY")
CPU_COUNT = multiprocessing.cpu_count()

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        'TEST': {
            'NAME': BASE_DIR / 'test_db.sqlite3',
        },
    }
}

CORS_ORIGIN_ALLOW_ALL = True
ALLOWED_HOSTS = ['*']
LOGGING = {
    'version': 1,
    'filters': {
        'require_debug_true': {
            '()': 'django.utils.log.RequireDebugTrue',
        }
    },
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'filters': ['require_debug_true'],
            'class': 'logging.StreamHandler',
        }
    },
    'loggers': {
        'django': {
            'level': 'INFO',
            'handlers': ['console'],
        },
        'django.db.backends': {
            'level': 'DEBUG',
            'handlers': ['console'],
        }
    }
}

# Dev-only (never staging/prod — this module is selected only when ENV is
# unset or 'dev', see crank/settings/__init__.py): raise allauth's per-IP
# login rate limit past what the seeded Django E2E tier needs. The tier
# signs in through the real /accounts/login/ page once per test (~18 times
# per spec run, all from 127.0.0.1); allauth's stock 30/minute ceiling then
# answers 429 Too Many Requests at a nondeterministic position whenever two
# runs (or a retry pass) land inside the same minute, flaking the
# auth-handoff step of an otherwise deterministic suite. Brute-force
# protection stays fully active: the per-account login_failed limiter is
# unchanged, and this override never ships outside dev.
ACCOUNT_RATE_LIMITS = {
    "login": "600/m/ip",
}
