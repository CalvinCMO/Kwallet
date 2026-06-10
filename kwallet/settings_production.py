"""
settings_production.py — KWallet v2 Production Settings
Set DJANGO_SETTINGS_MODULE=kwallet.settings_production on Railway/Render.

NOTE: This file is now deprecated. Use settings.py with environment variables instead.
Keeping this for backward compatibility, but the base settings.py handles all
production configuration automatically via env vars.
"""
from .settings import *
import os
import dj_database_url

DEBUG      = False
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')

if not SECRET_KEY:
    raise ValueError('DJANGO_SECRET_KEY environment variable is required in production.')

_hosts = os.environ.get('ALLOWED_HOSTS_LIST', '')
ALLOWED_HOSTS = [h.strip() for h in _hosts.split(',') if h.strip()]

_origins = os.environ.get('CSRF_TRUSTED_ORIGINS_LIST', '')
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _origins.split(',') if o.strip()]

# PostgreSQL — use DATABASE_URL from Railway with fallback to individual PG env vars
_db_url = os.environ.get('DATABASE_URL')

if _db_url:
    DATABASES = {
        'default': dj_database_url.parse(
            _db_url,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
else:
    # Fallback to individual PostgreSQL environment variables
    DATABASES = {
        'default': {
            'ENGINE':       'django.db.backends.postgresql',
            'NAME':         os.environ.get('PGDATABASE', ''),
            'USER':         os.environ.get('PGUSER', ''),
            'PASSWORD':     os.environ.get('PGPASSWORD', ''),
            'HOST':         os.environ.get('PGHOST', ''),
            'PORT':         os.environ.get('PGPORT', '5432'),
            'CONN_MAX_AGE': 60,
            'OPTIONS':      {'sslmode': 'require'},
        }
    }

# Static files — WhiteNoise already configured in base settings.py
# DO NOT add WhiteNoise middleware here — it's already in MIDDLEWARE at line 107
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
SECURE_HSTS_PRELOAD            = True
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
