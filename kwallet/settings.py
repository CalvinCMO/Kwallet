"""
settings.py — KWallet v2 Development Settings
"""
from pathlib import Path
import os
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY',
    'django-insecure-dev-only-kwallet-v2-change-in-production')

DEBUG = True
ALLOWED_HOSTS = ['*']

CSRF_TRUSTED_ORIGINS = [
    'http://kwallet-production-c0bd.up.railway.app',
    'http://127.0.0.1:8000',
    'http://localhost:8000',
]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

_ngrok = os.getenv('NGROK_URL')
if _ngrok and _ngrok not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(_ngrok)

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'wallet',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF    = 'kwallet.urls'
WSGI_APPLICATION = 'kwallet.wsgi.application'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.debug',
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
    ]},
}]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME':   BASE_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = 'en-us'
TIME_ZONE     = 'Africa/Nairobi'
USE_I18N = True
USE_TZ   = True

STATIC_URL       = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

SESSION_COOKIE_AGE         = 3600
SESSION_SAVE_EVERY_REQUEST = True

CACHES = {
    'default': {
        'BACKEND':  'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'kwallet-v2-cache',
        'TIMEOUT':  3600,
    }
}

EXCHANGE_RATE_CACHE_TTL = 3600

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name} — {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class':     'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'settlement_file': {
            # Writes every sweep and reconciliation event to a dedicated log.
            # In production point this at /var/log/kwallet/settlement.log
            'class':     'logging.FileHandler',
            'filename':  BASE_DIR / 'logs' / 'settlement.log',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'wallet': {
            'handlers':  ['console'],
            'level':     'DEBUG',
            'propagate': False,
        },
        # Settlement engine gets its own logger + file so sweeps/reconciliation
        # are always auditable independently of the general app log.
        'wallet.settlement': {
            'handlers':  ['console', 'settlement_file'],
            'level':     'INFO',
            'propagate': False,
        },
        'django': {
            'handlers':  ['console'],
            'level':     'INFO',
            'propagate': False,
        },
    },
}

MPESA_CONFIG = {
    'CONSUMER_KEY':          os.getenv('MPESA_CONSUMER_KEY',    ''),
    'CONSUMER_SECRET':       os.getenv('MPESA_CONSUMER_SECRET', ''),
    'SHORTCODE':             os.getenv('MPESA_SHORTCODE',        '174379'),
    'PASSKEY':               os.getenv('MPESA_PASSKEY',          ''),
    'CALLBACK_URL':          os.getenv('MPESA_CALLBACK_URL',     'http://localhost:8000/mpesa/callback/'),
    'INITIATOR_NAME':        os.getenv('MPESA_INITIATOR_NAME',   ''),
    'SECURITY_CREDENTIAL':   os.getenv('MPESA_SECURITY_CREDENTIAL', ''),
    'ENVIRONMENT':           os.getenv('MPESA_ENVIRONMENT',      'sandbox'),
    'USE_MOCK':              os.getenv('MPESA_USE_MOCK', 'True').lower() == 'true',
    'TIMEOUT':               int(os.getenv('MPESA_TIMEOUT', '60')),
}
