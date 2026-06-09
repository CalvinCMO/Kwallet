# KWallet — Multi-Currency Wallet with M-Pesa Integration

A full Django web application for managing multi-currency balances with
Safaricom M-Pesa STK Push (deposit) and B2C (withdrawal) integration.

---

## Features

| Feature | Details |
|---|---|
| Multi-currency balances | USD, EUR, GBP, JPY, KES |
| Secure PIN auth | bcrypt-hashed PINs, no plaintext stored |
| Deposit / Withdraw | Manual balance credits and debits |
| Currency Exchange | Live-rate conversion between any pair |
| P2P Transfer | Send to any other KWallet user by phone |
| M-Pesa Deposit | STK Push — customer pays into KES balance |
| M-Pesa Withdrawal | B2C — wallet sends KES to customer's phone |
| Transaction History | Filterable full audit trail |
| Admin Panel | Django admin for all models |

---

## Project Structure

```
kwallet/
├── manage.py
├── requirements.txt
├── kwallet/
│   ├── settings.py          # Django settings + M-Pesa config
│   ├── urls.py
│   └── wsgi.py
└── wallet/
    ├── models.py            # Wallet, CurrencyBalance, Transaction, MpesaTransaction
    ├── views.py             # All request handlers
    ├── forms.py             # Form validation
    ├── mpesa.py             # Daraja API client (OAuth, STK Push, B2C, callback parser)
    ├── admin.py
    ├── urls.py
    └── templates/wallet/
        ├── base.html
        ├── login.html
        ├── register.html
        ├── dashboard.html
        ├── deposit.html
        ├── withdraw.html
        ├── exchange.html
        ├── p2p.html
        ├── mpesa_deposit.html
        ├── mpesa_withdraw.html
        ├── mpesa_pending.html
        └── transactions.html
```

---

## Quick Start

### 1. Clone / extract project

```bash
cd kwallet
```

### 2. Create virtual environment and install dependencies

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run database migrations

```bash
python manage.py makemigrations wallet
python manage.py migrate
```

### 4. Create a Django superuser (for admin panel)

```bash
python manage.py createsuperuser
```

### 5. Start the development server

```bash
python manage.py runserver
```

Visit: http://127.0.0.1:8000

---

## M-Pesa Setup (Safaricom Daraja)

### Step 1 — Get Daraja credentials

1. Register at https://developer.safaricom.co.ke
2. Create an app — it gives you **Consumer Key** and **Consumer Secret**
3. Note your **Business Shortcode** and **Passkey** (from the STK Push test credentials)

### Step 2 — Configure credentials

Set environment variables (recommended for production):

```bash
export MPESA_CONSUMER_KEY="your_consumer_key"
export MPESA_CONSUMER_SECRET="your_consumer_secret"
export MPESA_SHORTCODE="174379"
export MPESA_PASSKEY="your_passkey"
export MPESA_CALLBACK_URL="https://yourdomain.com/mpesa/callback/"
export MPESA_ENVIRONMENT="sandbox"   # change to 'production' when ready
```

Or edit `kwallet/settings.py` directly under `MPESA_CONFIG`.

### Step 3 — Expose callback URL (development)

Safaricom needs to reach your server. Use ngrok in development:

```bash
pip install pyngrok
ngrok http 8000
```

Copy the HTTPS URL (e.g. `https://abc123.ngrok.io`) and set:

```bash
export MPESA_CALLBACK_URL="https://abc123.ngrok.io/mpesa/callback/"
```

### Step 4 — For B2C (Withdrawals)

B2C requires additional credentials:
- `INITIATOR_NAME` — your Daraja API operator username
- `SECURITY_CREDENTIAL` — encrypted password from Daraja portal

Add these to `settings.py` MPESA_CONFIG:

```python
'INITIATOR_NAME': 'testapi',
'SECURITY_CREDENTIAL': 'your_encrypted_credential',
```

> **Sandbox testing**: Use the Daraja sandbox simulator at
> https://developer.safaricom.co.ke/docs to test STK Push without real money.

---

## URL Reference

| URL | Name | Description |
|---|---|---|
| `/` | `login` | Login page |
| `/register/` | `register` | Create wallet |
| `/dashboard/` | `dashboard` | Main dashboard |
| `/deposit/` | `deposit` | Manual deposit |
| `/withdraw/` | `withdraw` | Manual withdrawal |
| `/exchange/` | `exchange` | Currency exchange |
| `/transfer/` | `p2p` | P2P transfer |
| `/transactions/` | `transactions` | Transaction history |
| `/mpesa/deposit/` | `mpesa_deposit` | M-Pesa STK Push |
| `/mpesa/withdraw/` | `mpesa_withdraw` | M-Pesa B2C withdrawal |
| `/mpesa/callback/` | `mpesa_callback` | Safaricom callback (POST) |
| `/admin/` | — | Django admin |

---

## Security Notes

- PINs are hashed with **bcrypt** — never stored in plaintext.
- National ID numbers are hashed with **SHA-256**.
- The M-Pesa callback endpoint is CSRF-exempt (required by Safaricom).
- In production, set `DEBUG = False` and generate a real `SECRET_KEY`.
- Use HTTPS in production — M-Pesa callbacks require it.

---

## Production Checklist

- [ ] Set `DEBUG = False` in settings.py
- [ ] Set a strong random `SECRET_KEY`
- [ ] Configure a real database (PostgreSQL recommended)
- [ ] Serve static files via a CDN or `whitenoise`
- [ ] Enable HTTPS (required for M-Pesa callbacks)
- [ ] Switch `MPESA_ENVIRONMENT` to `'production'`
- [ ] Set all M-Pesa credentials via environment variables
- [ ] Run `python manage.py collectstatic`
