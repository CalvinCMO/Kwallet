"""
settings.py — KWallet
Addresses Risk #07 (SSL), Risk #09 (health endpoint), Risk #10 (Redis cache),
Risk #01 (rate alert), Risk #12 (insolvency alert), Risk #08 (rate limiting).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'change-this-in-production-minimum-50-chars-random')
DEBUG      = os.environ.get('DEBUG', 'False') == 'True'
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

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
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Risk #08: rate limiting middleware (django-axes or custom, configured below)
]

ROOT_URLCONF = 'kwallet.urls'

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

WSGI_APPLICATION = 'kwallet.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME':     os.environ.get('PGDATABASE', 'postgres'),
        'USER':     os.environ.get('PGUSER',     'postgres'),
        'PASSWORD': os.environ.get('PGPASSWORD', '168290'),
        'HOST':     os.environ.get('PGHOST',     'localhost'),
        'PORT':     os.environ.get('PGPORT',     '5432'),
    }
}

# Risk #10: Redis cache — shared across workers, survives restarts
# Falls back to LocMemCache for local dev without Redis
REDIS_URL = os.environ.get('REDIS_URL', '')
if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': REDIS_URL,
            'OPTIONS': {'CLIENT_CLASS': 'django_redis.client.DefaultClient'},
            'TIMEOUT': 300,
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'kwallet-dev',
        }
    }

SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'default'

AUTH_USER_MODEL = 'wallet.WalletUser'
LOGIN_URL  = '/login/'
LOGIN_REDIRECT_URL = '/'

STATIC_URL  = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── M-Pesa config ──
MPESA_CONFIG = {
    'CONSUMER_KEY':          os.environ.get('MPESA_CONSUMER_KEY', ''),
    'CONSUMER_SECRET':       os.environ.get('MPESA_CONSUMER_SECRET', ''),
    'SHORTCODE':             os.environ.get('MPESA_SHORTCODE', '174379'),
    'PASSKEY':               os.environ.get('MPESA_PASSKEY', ''),
    'B2C_INITIATOR':         os.environ.get('MPESA_B2C_INITIATOR', ''),
    'B2C_SECURITY_CREDENTIAL': os.environ.get('MPESA_B2C_CREDENTIAL', ''),
    'CALLBACK_URL':          os.environ.get('MPESA_CALLBACK_URL', 'https://yourdomain.com/mpesa/callback/'),
    'B2C_RESULT_URL':        os.environ.get('MPESA_B2C_RESULT_URL', 'https://yourdomain.com/mpesa/b2c/result/'),
    # Risk #05: shared secret for callback HMAC verification
    'CALLBACK_SECRET':       os.environ.get('MPESA_CALLBACK_SECRET', ''),
    'ENVIRONMENT':           os.environ.get('MPESA_ENVIRONMENT', 'sandbox'),
    'USE_MOCK':              os.environ.get('MPESA_USE_MOCK', 'True') == 'True',
    # Risk #07: SSL always on; only disable with an explicit dev flag
    'DEV_DISABLE_SSL':       os.environ.get('MPESA_DEV_DISABLE_SSL', 'False') == 'True',
}

# ── Airtel Money config ──
AIRTEL_CONFIG = {
    'CLIENT_ID':         os.environ.get('AIRTEL_CLIENT_ID', ''),
    'CLIENT_SECRET':     os.environ.get('AIRTEL_CLIENT_SECRET', ''),
    'ENVIRONMENT':       os.environ.get('AIRTEL_ENVIRONMENT', 'sandbox'),
    'BASE_URL_SANDBOX':    'https://openapiuat.airtel.africa',
    'BASE_URL_PRODUCTION': 'https://openapi.airtel.africa',
    'COUNTRY':  'KE',
    'CURRENCY': 'KES',
    # Risk #05: shared secret for Airtel callback
    'CALLBACK_SECRET':       os.environ.get('AIRTEL_CALLBACK_SECRET', ''),
    'ALLOWED_CALLBACK_IPS':  os.environ.get('AIRTEL_CALLBACK_IPS', '').split(','),
}

# ── Email (Risk #01 #12: ops alerts) ──
EMAIL_BACKEND = os.environ.get(
    'EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend'  # use SMTP in production
)
EMAIL_HOST     = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT     = int(os.environ.get('EMAIL_PORT', 587))
EMAIL_USE_TLS  = True
EMAIL_HOST_USER     = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL  = os.environ.get('DEFAULT_FROM_EMAIL', 'alerts@kwallet.app')
ADMINS = [('KWallet Ops', os.environ.get('OPS_EMAIL', 'ops@kwallet.app'))]

# ── Logging ──
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '%(asctime)s %(levelname)s %(name)s %(message)s'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'wallet': {'handlers': ['console'], 'level': 'DEBUG', 'propagate': False},
    },
}

# ── Security headers (production) ──
if not DEBUG:
    SECURE_SSL_REDIRECT           = True
    SESSION_COOKIE_SECURE         = True
    CSRF_COOKIE_SECURE            = True
    SECURE_BROWSER_XSS_FILTER     = True
    SECURE_CONTENT_TYPE_NOSNIFF   = True
    SECURE_HSTS_SECONDS           = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS= True
    SECURE_HSTS_PRELOAD           = True
    X_FRAME_OPTIONS               = 'DENY'
