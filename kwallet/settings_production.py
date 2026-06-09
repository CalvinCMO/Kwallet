"""
settings_production.py — KWallet v2 Production Settings
Set DJANGO_SETTINGS_MODULE=kwallet.settings_production on Railway/Render.
"""
from .settings import *
import os

DEBUG      = False
SECRET_KEY = os.environ['DJANGO_SECRET_KEY']

_hosts = os.environ.get('ALLOWED_HOSTS_LIST', '')
ALLOWED_HOSTS = [h.strip() for h in _hosts.split(',') if h.strip()]

_origins = os.environ.get('CSRF_TRUSTED_ORIGINS_LIST', '')
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _origins.split(',') if o.strip()]

# PostgreSQL
DATABASES = {
    'default': {
        'ENGINE':       'django.db.backends.postgresql',
        'NAME':         os.environ['PGDATABASE'],
        'USER':         os.environ['PGUSER'],
        'PASSWORD':     os.environ['PGPASSWORD'],
        'HOST':         os.environ['PGHOST'],
        'PORT':         os.environ.get('PGPORT', '5432'),
        'CONN_MAX_AGE': 60,
        'OPTIONS':      {'sslmode': 'require'},
    }
}

# WhiteNoise for static files
MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# DB cache (no Redis needed on Railway free tier)
CACHES = {
    'default': {
        'BACKEND':  'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'kwallet_cache_table',
        'TIMEOUT':  3600,
    }
}

# M-Pesa production credentials (override dev defaults)
MPESA_CONFIG.update({
    'CONSUMER_KEY':        os.environ.get('MPESA_CONSUMER_KEY', ''),
    'CONSUMER_SECRET':     os.environ.get('MPESA_CONSUMER_SECRET', ''),
    'SHORTCODE':           os.environ.get('MPESA_SHORTCODE', ''),
    'PASSKEY':             os.environ.get('MPESA_PASSKEY', ''),
    'CALLBACK_URL':        os.environ.get('MPESA_CALLBACK_URL', ''),
    'INITIATOR_NAME':      os.environ.get('MPESA_INITIATOR_NAME', ''),
    'SECURITY_CREDENTIAL': os.environ.get('MPESA_SECURITY_CREDENTIAL', ''),
    'ENVIRONMENT':         os.environ.get('MPESA_ENVIRONMENT', 'production'),
    'USE_MOCK':            False,
    'TIMEOUT':             60,
})

# Security headers
SECURE_SSL_REDIRECT            = True
SECURE_PROXY_SSL_HEADER        = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_COOKIE_SECURE          = True
CSRF_COOKIE_SECURE             = True
SECURE_HSTS_SECONDS            = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_CONTENT_TYPE_NOSNIFF    = True
X_FRAME_OPTIONS                = 'DENY'

# Production logging to stdout (Railway captures these)
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'prod': {'format': '{levelname} {asctime} {module} {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'prod'},
    },
    'loggers': {
        'wallet': {'handlers': ['console'], 'level': 'INFO',    'propagate': False},
        'django': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
}
