"""
settings.py — KWallet v2
========================
Single settings file for both local development and Railway deployment.

Environment is driven entirely by env vars — no separate settings_production.py
needed.  Set these in Railway's "Variables" panel:

  REQUIRED IN PRODUCTION
  ──────────────────────
  DJANGO_SECRET_KEY          Strong random string (never the dev default)
  DATABASE_URL               Injected automatically by Railway Postgres plugin
  RAILWAY_PUBLIC_DOMAIN      Injected automatically by Railway (e.g. kwallet.up.railway.app)
  DJANGO_DEBUG               Set to 'False' in production (default: False)

  MPESA
  ─────
  MPESA_CONSUMER_KEY / MPESA_CONSUMER_SECRET
  MPESA_SHORTCODE / MPESA_PASSKEY
  MPESA_CALLBACK_URL         Must be your live Railway HTTPS URL
  MPESA_INITIATOR_NAME / MPESA_SECURITY_CREDENTIAL
  MPESA_ENVIRONMENT          'production' or 'sandbox'
  MPESA_USE_MOCK             'False' in production

  OPTIONAL
  ────────
  NGROK_URL                  Local tunnel for M-Pesa callbacks during dev
  DJANGO_ALLOWED_HOSTS       Extra comma-separated hosts beyond Railway domain
"""

from pathlib import Path
import os
import dj_database_url

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
except ImportError:
    pass

# ── Core ──────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-dev-only-kwallet-v2-change-in-production',
)

# DEBUG is False by default — must be explicitly opted in for local dev.
DEBUG = os.environ.get('DJANGO_DEBUG', 'False').strip().lower() == 'true'

# ── Hosts & CSRF ──────────────────────────────────────────────────────────────

# Railway injects RAILWAY_PUBLIC_DOMAIN automatically (e.g. kwallet.up.railway.app)
_railway_domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '')

ALLOWED_HOSTS = ['127.0.0.1', 'localhost']
if _railway_domain:
    ALLOWED_HOSTS.append(_railway_domain)

# Allow extra hosts via env var (comma-separated)
_extra_hosts = os.environ.get('DJANGO_ALLOWED_HOSTS', '')
if _extra_hosts:
    ALLOWED_HOSTS.extend(h.strip() for h in _extra_hosts.split(',') if h.strip())

# Trust Railway's HTTPS proxy header so Django sees secure requests correctly.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

CSRF_TRUSTED_ORIGINS = [
    'http://127.0.0.1:8000',
    'http://localhost:8000',
]
if _railway_domain:
    CSRF_TRUSTED_ORIGINS.append(f'https://{_railway_domain}')

_ngrok = os.environ.get('NGROK_URL', '')
if _ngrok and _ngrok not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(_ngrok)

# ── Security headers (active when DEBUG=False) ────────────────────────────────

if not DEBUG:
    SECURE_SSL_REDIRECT              = True   # HTTP → HTTPS redirect
    SESSION_COOKIE_SECURE            = True   # Session cookie over HTTPS only
    CSRF_COOKIE_SECURE               = True   # CSRF cookie over HTTPS only
    SECURE_HSTS_SECONDS              = 31536000  # 1 year HSTS
    SECURE_HSTS_INCLUDE_SUBDOMAINS   = True
    SECURE_HSTS_PRELOAD              = True
    SECURE_CONTENT_TYPE_NOSNIFF      = True
    X_FRAME_OPTIONS                  = 'DENY'

# ── Applications ──────────────────────────────────────────────────────────────

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
    'whitenoise.middleware.WhiteNoiseMiddleware',   # serves static files on Railway
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF     = 'kwallet.urls'
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

# ── Database ──────────────────────────────────────────────────────────────────
# Railway Postgres plugin sets DATABASE_URL automatically.
# Falls back to local SQLite for development when DATABASE_URL is absent.

_db_url = os.environ.get('DATABASE_URL')

if _db_url:
    DATABASES = {
        'default': dj_database_url.parse(
            _db_url,
            conn_max_age=600,       # persistent connections — important on Railway
            conn_health_checks=True,
        )
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME':   BASE_DIR / 'db.sqlite3',
        }
    }

# ── Password validation ───────────────────────────────────────────────────────

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ── Internationalisation ──────────────────────────────────────────────────────

LANGUAGE_CODE = 'en-us'
TIME_ZONE     = 'Africa/Nairobi'
USE_I18N      = True
USE_TZ        = True

# ── Static files ──────────────────────────────────────────────────────────────
# WhiteNoise compresses and fingerprints static files so Railway can serve them
# without a separate CDN.

STATIC_URL       = '/static/'
STATIC_ROOT      = BASE_DIR / 'staticfiles'          # collectstatic target
STATICFILES_DIRS = [BASE_DIR / 'static']
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Sessions ──────────────────────────────────────────────────────────────────

SESSION_COOKIE_AGE         = 3600
SESSION_SAVE_EVERY_REQUEST = True

# ── Cache ─────────────────────────────────────────────────────────────────────
# LocMemCache is per-process — fine for single-worker Railway deploys.
# If you scale to multiple workers, swap this for a Redis cache and point
# CACHE_URL at Railway's Redis plugin.

CACHES = {
    'default': {
        'BACKEND':  'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'kwallet-v2-cache',
        'TIMEOUT':  3600,
    }
}

EXCHANGE_RATE_CACHE_TTL = 3600

# ── Logging ───────────────────────────────────────────────────────────────────
# Railway captures stdout/stderr, so all logs go to the console handler.
# The settlement file handler is kept for local auditing; on Railway the
# console stream is the durable audit trail.

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
            'class':     'logging.FileHandler',
            'filename':  BASE_DIR / 'logs' / 'settlement.log',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'wallet': {
            'handlers':  ['console'],
            'level':     'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
        },
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

# ── M-Pesa ────────────────────────────────────────────────────────────────────

MPESA_CONFIG = {
    'CONSUMER_KEY':        os.environ.get('MPESA_CONSUMER_KEY',         ''),
    'CONSUMER_SECRET':     os.environ.get('MPESA_CONSUMER_SECRET',      ''),
    'SHORTCODE':           os.environ.get('MPESA_SHORTCODE',             '174379'),
    'PASSKEY':             os.environ.get('MPESA_PASSKEY',               ''),
    'CALLBACK_URL':        os.environ.get('MPESA_CALLBACK_URL',
                               f'https://{_railway_domain}/mpesa/callback/'
                               if _railway_domain
                               else 'http://localhost:8000/mpesa/callback/'),
    'INITIATOR_NAME':      os.environ.get('MPESA_INITIATOR_NAME',       ''),
    'SECURITY_CREDENTIAL': os.environ.get('MPESA_SECURITY_CREDENTIAL',  ''),
    'ENVIRONMENT':         os.environ.get('MPESA_ENVIRONMENT',           'sandbox'),
    'USE_MOCK':            os.environ.get('MPESA_USE_MOCK', 'True').lower() == 'true',
    'TIMEOUT':             int(os.environ.get('MPESA_TIMEOUT',           '60')),
}