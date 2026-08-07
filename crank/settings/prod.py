# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
import os
import pymysql
import multiprocessing
import dj_db_conn_pool
from django.db import connection
from pathlib import Path

from crank.settings import REDIS_MASTER_URL

pymysql.install_as_MySQLdb()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEBUG = False
# NOTE: SECURE_SSL_REDIRECT must stay disabled. Production runs Django's dev
# server (`manage.py runserver`, HTTP-only) on port 8080 behind Cloudflare.
# Enabling SSL redirect here makes Django return 301 -> https for every
# request; Cloudflare then opens a TLS connection to the HTTP-only origin,
# the dev server cannot speak TLS, and the liveness probe fails -> crashloop.
# Enforce HTTPS at the Cloudflare edge ("Always Use HTTPS") instead.
SECRET_KEY = os.environ.get('SECRET_KEY')
CPU_COUNT = multiprocessing.cpu_count()

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = True

REDIS_SLAVE_URLS = os.environ.get("REDIS_SLAVE_URLS", "redis://redis-slave:6379/0").split(",")

SESSION_REDIS = {
    'ttl': 1800,  # 30 minutes in seconds
    'master': REDIS_MASTER_URL,
    'slaves': REDIS_SLAVE_URLS,
}

DATABASES = {
    'default': {
        'ENGINE': 'dj_db_conn_pool.backends.mysql',
        'NAME': os.environ.get('DB_NAME'),
        'HOST': os.environ.get('DB_HOST'),
        'PORT': os.environ.get('DB_PORT'),
        'USER': os.environ.get('DB_USER'),
        'PASSWORD': os.environ.get('DB_PASS'),
        'POOL_OPTIONS': {
            'POOL_SIZE': CPU_COUNT * 4,  # Maximum number of connections in the pool
            'POOL_RECYCLE': 3600,  # recycle connections after this many seconds
        },
    }
}
ALLOWED_HOSTS = ['*']
ACCOUNT_DEFAULT_HTTP_PROTOCOL = 'https'
# Cloudflare terminates TLS and forwards to the k8s NodePort over plain HTTP,
# so Django sees request.scheme == 'http'. Trust the proxy's forwarded header
# so is_secure() / build_absolute_uri() report https and the OAuth redirect_uri
# stays consistent between login initiation and the token-exchange callback.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'local': {
            'format': "[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s"
        },

        'verbose': {
            'format': '{message}',
            'style': '{',
        },
    },
    'filters': {
        'require_debug_false': {
            '()': 'django.utils.log.RequireDebugFalse',
        }
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'local',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'users': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.db.backends': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'mail_admins': {
            'level': 'ERROR',
            'filters': ['require_debug_false'],
            'class': 'django.utils.log.AdminEmailHandler'
        }
    }
}