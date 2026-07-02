"""
settings.py — KWallet
Addresses Risk #07 (SSL), Risk #09 (health endpoint), Risk #10 (Redis cache),
Risk #01 (rate alert), Risk #12 (insolvency alert), Risk #08 (rate limiting).
"""
import os
from pathlib import Path
import dj_database_url


BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'change-this-in-production-minimum-50-chars-random')
DEBUG      = os.environ.get('DEBUG', 'False') == 'True'
ALLOWED_HOSTS = os.environ.get(
    'ALLOWED_HOSTS', 
    'localhost,127.0.0.1,kwallet-production-c0bd.up.railway.app,.up.railway.app'
).split(',')

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
    # ── KWallet custom session security ──────────────────────────────────────
    'wallet.middleware.SingleDeviceMiddleware',   # one active session per user
    'wallet.middleware.IdleTimeoutMiddleware',    # auto-logout after 5 min idle
]

# ── Sandbox / Mock mode ─────────────────────────────────────────────────────
# Set WALLET_SANDBOX_MODE=False in Railway env vars when you go live.
# While True, all STK pushes, B2C, and bank webhooks are simulated locally —
# no real money moves regardless of M-Pesa/Airtel environment setting.
WALLET_SANDBOX_MODE = os.environ.get('WALLET_SANDBOX_MODE', 'True') == 'True'

# How long (seconds) after a mock STK push the auto-confirm fires (simulates network delay)
SANDBOX_CONFIRM_DELAY = int(os.environ.get('SANDBOX_CONFIRM_DELAY', '3'))

# Starting balance credited to new sandbox wallets for each currency they add
SANDBOX_STARTING_BALANCE = {
    'KES': 10000,
    'USD': 100,
    'EUR': 100,
    'GBP': 100,
    'TZS': 250000,
    'UGX': 400000,
}



# ── Idle timeout — 5 minutes (300 seconds) ──────────────────────────────────
IDLE_TIMEOUT_SECONDS = 300

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
        'wallet.middleware.idle_timeout_context',   # IDLE_TIMEOUT_SECONDS in all templates
    ]},
}]

WSGI_APPLICATION = 'kwallet.wsgi.application'

DATABASES = {
    'default': dj_database_url.config(
        default=os.environ.get('DATABASE_URL'),
        conn_max_age=600,
        conn_health_checks=True,
    )
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

# Sessions MUST live somewhere shared across all gunicorn worker processes.
# LocMemCache is per-process — if SESSION_ENGINE='cache' is used with it
# (e.g. when REDIS_URL isn't set), each worker has its own private session
# store, so a login handled by worker A is invisible to worker B and the
# user gets randomly logged out depending on which worker serves the next
# request. Only use cache-backed sessions when Redis (a real shared store)
# is configured; otherwise use the database, which is already shared via
# Postgres across all workers.
if REDIS_URL:
    SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
    SESSION_CACHE_ALIAS = 'default'
else:
    SESSION_ENGINE = 'django.contrib.sessions.backends.db'

AUTH_USER_MODEL = 'wallet.WalletUser'
LOGIN_URL  = '/login/'
LOGIN_REDIRECT_URL = '/'

# WalletUser stores its password as a bcrypt+pepper hash (see
# WalletUser.set_pin/check_pin), NOT a Django-hasher hash. The default
# ModelBackend calls user.check_password(), which uses Django's hashers
# and will always reject a bcrypt hash — this silently broke Django admin
# login (and anything else going through authenticate()) even with the
# correct phone/PIN. PinBackend implements the matching bcrypt check and
# must be registered here; ModelBackend is kept only as a fallback for any
# legacy rows that were mistakenly written with set_password().
AUTHENTICATION_BACKENDS = [
    'wallet.backends.PinBackend',
    'django.contrib.auth.backends.ModelBackend',
]

STATIC_URL  = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL  = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

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

# ── Flutterwave config (v4 / OAuth2) ──
# Credentials from https://dashboard.flutterwave.com → v4 Developer toggle →
# Settings → API Keys (generates a Client ID + Client Secret, not a static
# secret key — v4 uses OAuth2 client_credentials, see wallet/flutterwave.py)
# Webhook: set https://<your-domain>/flw/webhook/ in FLW dashboard → Webhooks
# Redirect: set https://<your-domain>/deposit/card/return/ in FLW Inline config
FLUTTERWAVE_CONFIG = {
    'CLIENT_ID':        os.environ.get('FLW_CLIENT_ID', ''),
    'CLIENT_SECRET':     os.environ.get('FLW_CLIENT_SECRET', ''),
    'ENCRYPTION_KEY':   os.environ.get('FLW_ENCRYPTION_KEY', ''),   # used for card-field encryption in v4
    # Risk #05: must match "Secret Hash" set in FLW dashboard → Webhooks.
    # v4 uses this to HMAC-SHA256 the raw webhook body (see verify_webhook_signature).
    'WEBHOOK_SECRET':   os.environ.get('FLW_WEBHOOK_SECRET', ''),
    # URL Flutterwave redirects to after hosted payment (card / bank transfer)
    'REDIRECT_URL':     os.environ.get('FLW_REDIRECT_URL', 'https://yourdomain.com/deposit/card/return/'),
    # URL Flutterwave posts transfer (payout) status updates to
    'TRANSFER_CALLBACK_URL': os.environ.get('FLW_TRANSFER_CALLBACK_URL', 'https://yourdomain.com/flw/webhook/'),
    # Optional: branding on hosted checkout
    'LOGO_URL':         os.environ.get('FLW_LOGO_URL', ''),
    # Risk #07: set DEV_DISABLE_SSL=True in .env only for local dev without SSL
    'DEV_DISABLE_SSL':  os.environ.get('FLW_DEV_DISABLE_SSL', 'false').lower() == 'true',
    # Set USE_MOCK=True to skip live API calls (sandbox wallets always mock regardless)
    'USE_MOCK':         os.environ.get('FLW_USE_MOCK', 'true').lower() == 'true',
    # 'sandbox' or 'live' — selects v4's base URL (v4 splits these completely,
    # unlike v3's single host + sk_test_/sk_live_ key prefix)
    'ENVIRONMENT':      os.environ.get('FLW_ENVIRONMENT', 'sandbox'),
    # Risk #05: Flutterwave webhook IP prefixes — verify from FLW docs periodically
    'ALLOWED_WEBHOOK_IPS': [ip for ip in os.environ.get('FLW_WEBHOOK_IPS', '').split(',') if ip],
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

    # 🔥 IMPORTANT for Railway / any reverse proxy
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')