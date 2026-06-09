# KWallet v2 — Complete Setup & Deployment Guide

## What's in this release

| Feature | Status |
|---|---|
| 5 East African currencies for every user (KES/TZS/UGX/RWF/ETB) | ✅ |
| User chooses 5 additional international currencies at registration | ✅ |
| Add more currencies any time from dashboard | ✅ |
| M-Pesa STK Push deposit (free) | ✅ |
| M-Pesa B2C withdrawal (1% fee) | ✅ |
| In-wallet currency exchange (1.5% fee) | ✅ |
| P2P transfer to any KWallet user (0.5% fee) | ✅ |
| Fee ledger for company revenue accounting | ✅ |
| Live exchange rates (ECB via frankfurter.app) | ✅ |
| 19 supported currencies total | ✅ |
| Country-aware registration (KE/TZ/UG/RW/ET) | ✅ |
| White + green bright design system | ✅ |
| Production-ready settings structure | ✅ |

---

## Part 1 — Local Development Setup

### Prerequisites
- Python 3.10 or higher
- pip
- Git (optional but recommended)

### Step 1: Extract and enter the project

```bash
# Extract the zip
unzip kwallet_v2.zip
cd kwallet_v2
```

### Step 2: Create a virtual environment

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

**Mac/Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Install dependencies

```bash
pip install -r requirements.txt
```

### Step 4: Create your .env file

Copy the example and fill in your values:

```bash
cp .env.example .env
```

Open `.env` and set:

```env
DJANGO_SECRET_KEY=any-long-random-string-here
MPESA_CONSUMER_KEY=your_daraja_consumer_key
MPESA_CONSUMER_SECRET=your_daraja_consumer_secret
MPESA_SHORTCODE=174379
MPESA_PASSKEY=your_daraja_passkey
MPESA_CALLBACK_URL=http://localhost:8000/mpesa/callback/
MPESA_ENVIRONMENT=sandbox
MPESA_USE_MOCK=True
MPESA_TIMEOUT=60
NGROK_URL=
```

> Leave `MPESA_USE_MOCK=True` for now — you can develop and test everything without Safaricom.

### Step 5: Run migrations

```bash
python manage.py makemigrations wallet
python manage.py migrate
```

### Step 6: Create an admin user (optional)

```bash
python manage.py createsuperuser
```

### Step 7: Start the server

```bash
python manage.py runserver
```

Visit `http://127.0.0.1:8000` — you'll see the login page.

### Step 8: Register your first wallet

1. Go to `/register/`
2. Enter your name, phone number, and country
3. Choose a PIN
4. Select 5 international currencies (e.g. USD, EUR, GBP, JPY, CNY)
5. Submit — you'll have 10 currency balances ready (5 EA + 5 chosen)

---

## Part 2 — Real M-Pesa STK Push Setup

### Prerequisites
- A Safaricom M-Pesa registered phone number
- A Daraja developer account at `developer.safaricom.co.ke`
- ngrok installed

### Step 1: Get your Daraja credentials

1. Login at `developer.safaricom.co.ke`
2. Click **My Apps** → select or create your app
3. Enable **Lipa Na M-Pesa Sandbox** and **M-Pesa Sandbox**
4. Copy your **Consumer Key** and **Consumer Secret**
5. Go to **APIs** → **M-Pesa Express** → copy the **Online Passkey**

### Step 2: Start ngrok

Open a second terminal window:

```bash
ngrok http 8000
```

Copy the `https://xxxx.ngrok-free.app` URL shown.

### Step 3: Update your .env

```env
MPESA_CONSUMER_KEY=paste_your_consumer_key
MPESA_CONSUMER_SECRET=paste_your_consumer_secret
MPESA_SHORTCODE=174379
MPESA_PASSKEY=bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919
MPESA_CALLBACK_URL=https://xxxx.ngrok-free.app/mpesa/callback/
MPESA_ENVIRONMENT=sandbox
MPESA_USE_MOCK=False
NGROK_URL=https://xxxx.ngrok-free.app
```

> The passkey shown above is Safaricom's public sandbox passkey.
> Replace with yours if different.

### Step 4: Restart Django

```bash
python manage.py runserver
```

### Step 5: Test

1. Login and click **M-Pesa Deposit**
2. Enter KES 1 (minimum test amount)
3. Click **Send STK Push**
4. Your phone rings with M-Pesa PIN prompt
5. Enter PIN → KES balance updates

> **Note:** In sandbox mode, the STK Push goes to Safaricom's test number, not your real phone. To test on your real phone you need production Go-Live credentials from Safaricom.

---

## Part 3 — Production Deployment on Railway

Railway is the recommended host — it gives you a public HTTPS URL (required for M-Pesa callbacks) and free PostgreSQL.

### Step 1: Push to GitHub

```bash
git init
git add .
git commit -m "KWallet v2 initial"
git remote add origin https://github.com/YOUR_USERNAME/kwallet.git
git push -u origin main
```

Make sure `.gitignore` includes:
```
.env
db.sqlite3
__pycache__/
*.pyc
staticfiles/
venv/
```

### Step 2: Create a Railway project

1. Go to `railway.app` → **New Project**
2. Click **Deploy from GitHub repo** → select your kwallet repo
3. Railway detects Python automatically

### Step 3: Add PostgreSQL database

1. In Railway dashboard → click **+ New**
2. Select **Database** → **PostgreSQL**
3. Railway creates the DB and links it automatically

### Step 4: Create production settings file

Create `kwallet/settings_production.py`:

```python
from .settings import *
import os

DEBUG = False
SECRET_KEY = os.environ['DJANGO_SECRET_KEY']

_hosts = os.environ.get('ALLOWED_HOSTS_LIST', '')
ALLOWED_HOSTS = [h.strip() for h in _hosts.split(',') if h.strip()]

_origins = os.environ.get('CSRF_TRUSTED_ORIGINS_LIST', '')
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _origins.split(',') if o.strip()]

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

MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'
STATIC_ROOT = BASE_DIR / 'staticfiles'

CACHES = {
    'default': {
        'BACKEND':  'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'kwallet_cache_table',
        'TIMEOUT':  3600,
    }
}

SECURE_SSL_REDIRECT            = True
SECURE_PROXY_SSL_HEADER        = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_COOKIE_SECURE          = True
CSRF_COOKIE_SECURE             = True
SECURE_HSTS_SECONDS            = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_CONTENT_TYPE_NOSNIFF    = True
```

### Step 5: Create build.sh

```bash
#!/usr/bin/env bash
set -o errexit
pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate
python manage.py createcachetable 2>/dev/null || true
echo "Build complete"
```

Make it executable:
```bash
chmod +x build.sh
```

### Step 6: Set environment variables in Railway

In Railway dashboard → your service → **Variables** tab, add:

| Variable | Value |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `kwallet.settings_production` |
| `DJANGO_SECRET_KEY` | Generate at djecrety.ir |
| `ALLOWED_HOSTS_LIST` | `your-app.railway.app` |
| `CSRF_TRUSTED_ORIGINS_LIST` | `https://your-app.railway.app` |
| `MPESA_CONSUMER_KEY` | from Daraja portal |
| `MPESA_CONSUMER_SECRET` | from Daraja portal |
| `MPESA_SHORTCODE` | your shortcode |
| `MPESA_PASSKEY` | your passkey |
| `MPESA_CALLBACK_URL` | `https://your-app.railway.app/mpesa/callback/` |
| `MPESA_ENVIRONMENT` | `production` (after Go-Live) or `sandbox` |
| `MPESA_USE_MOCK` | `False` |
| `PGDATABASE` | auto-set by Railway PostgreSQL |
| `PGUSER` | auto-set by Railway PostgreSQL |
| `PGPASSWORD` | auto-set by Railway PostgreSQL |
| `PGHOST` | auto-set by Railway PostgreSQL |

### Step 7: Set start command in Railway

In Railway → your service → **Settings** → **Start Command**:

```
gunicorn kwallet.wsgi --workers 2 --bind 0.0.0.0:$PORT --log-file -
```

And **Build Command**:
```
bash build.sh
```

### Step 8: Deploy

Railway deploys automatically on every `git push`:

```bash
git add .
git commit -m "Add production settings"
git push
```

Railway shows build logs in real time. When done, your app is live at `https://your-app.railway.app`.

---

## Part 4 — M-Pesa Production Go-Live

### Step 1: Test in sandbox first

Before applying, run through these test cases on sandbox and take screenshots:

1. ✅ Successful STK Push deposit
2. ✅ Failed STK Push (cancel on phone)
3. ✅ Successful B2C withdrawal
4. ✅ B2C timeout with auto-refund

### Step 2: Apply for Go-Live on Daraja

1. Login → `developer.safaricom.co.ke`
2. Select your app → click **Go Live**
3. Upload screenshots of your test cases
4. Fill in the business details form
5. Safaricom reviews in 2–5 business days

### Step 3: After Go-Live approval

Update Railway environment variables:

```
MPESA_ENVIRONMENT=production
MPESA_CONSUMER_KEY=your_production_key
MPESA_CONSUMER_SECRET=your_production_secret
MPESA_SHORTCODE=your_production_shortcode
MPESA_PASSKEY=your_production_passkey
MPESA_CALLBACK_URL=https://your-app.railway.app/mpesa/callback/
```

Test with a real KES 1 deposit to confirm end-to-end.

---

## Part 5 — Adding New Payment Rails (Future Updates)

The codebase is architected for easy rail additions. To add MTN MoMo Uganda:

1. Create `wallet/rails/mtn_momo.py` implementing `deposit()`, `withdraw()`, `check_status()`
2. Add `MTN_MOMO_CONFIG` to `settings.py`
3. Add MTN MoMo views to `views.py` following the M-Pesa pattern
4. Add URL routes to `urls.py`
5. Add deposit/withdraw templates

No changes needed to models, forms, or the fee system — they're already designed for multi-rail.

---

## Part 6 — Fee Schedule Reference

| Transaction | Fee | Minimum | Notes |
|---|---|---|---|
| M-Pesa Deposit | **0%** | — | Free to encourage deposits |
| M-Pesa Withdrawal | **1%** | KES 10 | Deducted from KES balance |
| Currency Exchange | **1.5%** | — | On source currency amount |
| P2P Transfer | **0.5%** | KES 5 equivalent | Charged to sender only |
| Bank Deposit | **0.5%** | — | On received amount |
| Bank Withdrawal | **1.5%** | KES 50 equivalent | On withdrawal amount |

All fees are recorded in the `FeeRecord` table for revenue accounting.
View revenue totals via Django Admin → Fee Records.

---

## Part 7 — Monitoring & Maintenance

### Check app health
```
GET https://your-app.railway.app/health/
```
Returns: `{"status": "ok", "database": "ok", "environment": "production", "version": "2.0.0"}`

### View logs
Railway dashboard → your service → **Logs** tab shows real-time application logs including M-Pesa API calls.

### Apply database updates
After any model changes:
```bash
python manage.py makemigrations wallet
python manage.py migrate
```

Then `git push` — Railway runs migrations automatically via `build.sh`.

### Admin panel
Visit `https://your-app.railway.app/admin/` to manage wallets, view all transactions, fee records, and M-Pesa transaction history.
