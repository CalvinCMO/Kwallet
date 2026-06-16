"""
settings.py — KWallet
Addresses Risk #07 (SSL), Risk #09 (health endpoint), Risk #10 (Redis cache),
Risk #01 (rate alert), Risk #12 (insolvency alert), Risk #08 (rate limiting).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'change-this-in-production-minimum-50-chars-random')

DEBUG = os.environ.get('DEBUG', 'False') == 'True'

ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1,kwallet-production-c0bd.up.railway.app').split(',')

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
    'whitenoise.middleware.WhiteNoiseMiddleware',   # Must be early
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'kwallet.urls'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],   # Optional: good practice
    'APP_DIRS': True,
    'OPTIONS': {
        'context_processors': [
            'django.template.context_processors.debug',
            'django.template.context_processors.request',
            'django.contrib.auth.context_processors.auth',
            'django.contrib.messages.context_processors.messages',
        ],
    },
}]

WSGI_APPLICATION = 'kwallet.wsgi.application'

# ====================== DATABASE ======================
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('PGDATABASE', 'kwallet'),
        'USER': os.environ.get('PGUSER', 'kwallet'),
        'PASSWORD': os.environ.get('PGPASSWORD', ''),
        'HOST': os.environ.get('PGHOST', 'localhost'),
        'PORT': os.environ.get('PGPORT', '5432'),
    }
}

# ====================== STATIC FILES (Railway Fix) ======================
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# This prevents the "No directory at: /app/staticfiles/" warning
STATICFILES_DIRS = [
    BASE_DIR / 'static',   # Optional: for project-wide static files
]

STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# ====================== CACHE & SESSIONS ======================
REDIS_URL = os.environ.get('REDIS_URL', '')

if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': REDIS_URL,
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            },
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

# ====================== AUTH ======================
AUTH_USER_MODEL = 'wallet.WalletUser'
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ====================== MPESA / AIRTEL / EMAIL (unchanged) ======================
# ... [Keep all your MPESA_CONFIG, AIRTEL_CONFIG, EMAIL settings as they were] ...

MPESA_CONFIG = {
    'CONSUMER_KEY':          os.environ.get('MPESA_CONSUMER_KEY', ''),
    'CONSUMER_SECRET':       os.environ.get('MPESA_CONSUMER_SECRET', ''),
    'SHORTCODE':             os.environ.get('MPESA_SHORTCODE', '174379'),
    'PASSKEY':               os.environ.get('MPESA_PASSKEY', ''),
    'B2C_INITIATOR':         os.environ.get('MPESA_B2C_INITIATOR', ''),
    'B2C_SECURITY_CREDENTIAL': os.environ.get('MPESA_B2C_CREDENTIAL', ''),
    'CALLBACK_URL':          os.environ.get('MPESA_CALLBACK_URL', 'https://yourdomain.com/mpesa/callback/'),
    'B2C_RESULT_URL':        os.environ.get('MPESA_B2C_RESULT_URL', 'https://yourdomain.com/mpesa/b2c/result/'),
    'CALLBACK_SECRET':       os.environ.get('MPESA_CALLBACK_SECRET', ''),
    'ENVIRONMENT':           os.environ.get('MPESA_ENVIRONMENT', 'sandbox'),
    'USE_MOCK':              os.environ.get('MPESA_USE_MOCK', 'True') == 'True',
    'DEV_DISABLE_SSL':       os.environ.get('MPESA_DEV_DISABLE_SSL', 'False') == 'True',
}

AIRTEL_CONFIG = { ... }   # keep as is

EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
# ... rest of email + logging + security settings (keep as you had)

# ── Security headers (production) ──
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    X_FRAME_OPTIONS = 'DENY'