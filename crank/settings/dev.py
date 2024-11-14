# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import multiprocessing
from pathlib import Path
from django.core.cache.backends.redis import RedisCache
import os


BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEBUG = True
SECRET_KEY = os.environ["SECRET_KEY"]
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

