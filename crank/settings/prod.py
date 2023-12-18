import os
import pymysql
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
pymysql.install_as_MySQLdb()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEBUG = False
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.environ.get('DB_NAME'),
        'HOST': os.environ.get('DB_HOST'),
        'PORT': os.environ.get('DB_PORT'),
        'USER': os.environ.get('DB_USER'),
        'PASSWORD': os.environ.get('DB_PASS'),
        'OPTIONS': {'ssl': {'ca': os.environ.get('MYSQL_ATTR_SSL_CA')}}
    }
}
ALLOWED_HOSTS = ['crank.fyi']
LOGGING = {
    'version': 1,
    'filters': {
        'require_debug_true': {
            '()': 'django.utils.log.RequireDebugTrue',
        }
    },
    'handlers': {
        'console': {
            'level': 'INFO',
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
            'level': 'INFO',
            'handlers': ['console'],
        }
    }
}