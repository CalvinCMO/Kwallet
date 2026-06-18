"""
views.py — KWallet
All risks from the Risk Analysis addressed inline with # Risk #XX comments.
"""
import hashlib
import hmac
import json
import logging
import uuid
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .airtel import AirtelClient
from .models import (
    AirtelTransaction, BankTransaction, CompanyAccount, CurrencyBalance,
    MpesaTransaction, PoolLedger, QRPaymentRequest, SuspiciousActivityFlag,
    Transaction, Wallet, WalletLimit, WalletUser, PinResetToken,
    DAILY_WITHDRAW_LIMIT, MAX_CURRENCIES, STALE_RATE_MAX_EXCHANGE,
    BANK_WITHDRAW_FEE, get_withdraw_fee, get_send_fee, mask_phone, mask_name,
)
from .mpesa import MpesaClient
from .rates import get_rates, get_pair_rate, rates_are_stale

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Helpers / Decorators
# ─────────────────────────────────────────────

BANK_CHOICES = [
    ('KCB',     'KCB Kenya'),
    ('Equity',  'Equity Bank'),
    ('Coop',    'Co-operative Bank'),
    ('NCBA',    'NCBA Bank'),
    ('Stanbic', 'Stanbic Bank'),
    ('Absa',    'Absa Kenya'),
    ('IM',      'I&M Bank'),
    ('DTB',     'Diamond Trust Bank'),
    ('Family',  'Family Bank'),
    ('HFC',     'HF Group'),
    ('Gulf',    'Gulf African Bank'),
    ('Other',   'Other (PesaLink)'),
]

EA_CURRENCIES = [
    ('KES', 'Kenyan Shilling'), ('TZS', 'Tanzanian Shilling'),
    ('UGX', 'Ugandan Shilling'), ('RWF', 'Rwandan Franc'),
    ('ETB', 'Ethiopian Birr'),
]
INTL_CURRENCIES = [
    ('USD', 'US Dollar'), ('EUR', 'Euro'), ('GBP', 'Pound Sterling'),
    ('JPY', 'Japanese Yen'), ('CNY', 'Chinese Yuan'), ('AED', 'UAE Dirham'),
    ('INR', 'Indian Rupee'), ('CAD', 'Canadian Dollar'), ('AUD', 'Australian Dollar'),
    ('CHF', 'Swiss Franc'), ('ZAR', 'South African Rand'), ('NGN', 'Nigerian Naira'),
    ('GHS', 'Ghanaian Cedi'),
]


def wallet_required(view_fn):
    """Ensure user is authenticated and has a wallet."""
    @wraps(view_fn)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        try:
            wallet = request.user.wallet
        except Wallet.DoesNotExist:
            # Logging out (rather than just redirecting to 'login') avoids
            # an infinite loop: login_view redirects authenticated users
            # straight to 'dashboard', which would bounce back here.
            from django.contrib.auth import logout as _auth_logout
            _auth_logout(request)
            messages.error(request, 'No wallet found. Please contact support.')
            return redirect('login')
        return view_fn(request, wallet, *args, **kwargs)
    return wrapper


def kyc_required(view_fn):
    """Risk #15: block view entirely if KYC is not verified."""
    @wraps(view_fn)
    def wrapper(request, wallet, *args, **kwargs):
        if wallet.kyc_status != 'verified':
            messages.warning(request, 'Identity verification required before you can perform this action.')
            return redirect('kyc_start')
        return view_fn(request, wallet, *args, **kwargs)
    return wrapper


def get_client_ip(request):
    """
    Risk #03/#05/#08: X-Forwarded-For is a comma-separated list of IPs when
    requests pass through one or more proxies (e.g. Railway's edge) — the
    client's original IP is the first entry. Using the raw header value as
    a rate-limit key or for IP-allowlist checks is incorrect: it produces
    invalid/inconsistent cache keys and can break prefix-based IP matching.
    """
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def _rate_limit_key(prefix, identifier):
    return f"ratelimit:{prefix}:{identifier}"


def _check_rate_limit(prefix, identifier, max_attempts, window_seconds):
    """Risk #03 & #08: generic rate limiter using cache."""
    key = _rate_limit_key(prefix, identifier)
    attempts = cache.get(key, 0)
    if attempts >= max_attempts:
        return False, attempts
    cache.set(key, attempts + 1, timeout=window_seconds)
    return True, attempts + 1


def _dashboard_context(wallet):
    """Shared context for dashboard."""
    balances = wallet.currency_balances.all().order_by('currency')
    daily_withdrawn = float(wallet.get_daily_withdrawn())
    daily_pct = wallet.get_daily_pct()
    recent_txns = wallet.transactions.all()[:8]
    total_value = sum(float(cb.balance) for cb in balances if cb.currency == wallet.home_currency)
    return {
        'wallet': wallet,
        'balances': balances,
        'home_currency': wallet.home_currency,
        'recent_txns': recent_txns,
        'total_value': total_value,
        'rates_stale': rates_are_stale(),
        'daily_withdrawn': daily_withdrawn,
        'daily_limit': DAILY_WITHDRAW_LIMIT,
        'daily_pct': daily_pct,
    }


def _get_or_create_limit(wallet):
    limit, _ = WalletLimit.objects.get_or_create(wallet=wallet)
    return limit


def _check_aml_velocity(wallet, amount_kes: Decimal):
    """Risk #16: flag structuring, velocity, round-number patterns."""
    today = timezone.now().date()
    from django.db.models import Count, Sum

    # Count transactions in last hour
    one_hour_ago = timezone.now() - timezone.timedelta(hours=1)
    hourly_count = wallet.transactions.filter(created_at__gte=one_hour_ago).count()
    if hourly_count >= 20:
        SuspiciousActivityFlag.objects.create(
            wallet=wallet,
            flag_type='velocity',
            description=f"More than 20 transactions in the last hour. Count={hourly_count}",
        )

    # Structuring: amounts clustering just below KES 10,000
    if 9000 <= amount_kes < 10000:
        SuspiciousActivityFlag.objects.create(
            wallet=wallet,
            flag_type='structuring',
            description=f"Transaction amount KES {amount_kes} is just below the KES 10,000 monitoring threshold.",
        )

    # Round numbers above threshold
    if amount_kes >= 50000 and amount_kes % 1000 == 0:
        SuspiciousActivityFlag.objects.create(
            wallet=wallet,
            flag_type='round_number',
            description=f"Large round-number transaction: KES {amount_kes}",
        )


# ─────────────────────────────────────────────
# Auth Views
# ─────────────────────────────────────────────

def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        phone      = request.POST.get('phone', '').strip()
        first_name = request.POST.get('first_name', '').strip()
        last_name  = request.POST.get('last_name', '').strip()
        pin        = request.POST.get('pin', '')
        pin_confirm = request.POST.get('pin_confirm', '')
        country    = request.POST.get('country', 'KE')

        errors = []
        # Risk #03: enforce 6-digit minimum PIN
        if len(pin) < 6:
            errors.append('PIN must be at least 6 digits.')
        if pin != pin_confirm:
            errors.append('PINs do not match.')
        if WalletUser.objects.filter(phone=phone).exists():
            errors.append('A wallet with this phone number already exists.')
        if not phone:
            errors.append('Phone number is required.')

        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, 'wallet/register.html', {
                'countries': [('KE','Kenya'),('TZ','Tanzania'),('UG','Uganda'),
                              ('RW','Rwanda'),('ET','Ethiopia'),('NG','Nigeria'),('GH','Ghana')],
            })

        try:
            with db_transaction.atomic():
                user = WalletUser.objects.create_user(
                    phone=phone, pin=pin,
                    first_name=first_name, last_name=last_name,
                )
                wallet_id = 'KW' + uuid.uuid4().hex[:10].upper()
                wallet = Wallet.objects.create(
                    wallet_id=wallet_id,
                    wallet_user=user,
                    wallet_id_str=wallet_id,
                    phone=phone,
                    home_currency='KES',
                    kyc_status='pending',
                )
                # Create default KES balance
                CurrencyBalance.objects.create(wallet=wallet, currency='KES', balance=0)
                WalletLimit.objects.create(wallet=wallet)

            auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, f'Welcome, {first_name}! Your wallet has been created. Please complete KYC to unlock all features.')
            return redirect('kyc_start')
        except Exception as e:
            logger.exception('Registration error')
            messages.error(request, 'Registration failed. Please try again.')

    return render(request, 'wallet/register.html', {
        'countries': [('KE','Kenya'),('TZ','Tanzania'),('UG','Uganda'),
                      ('RW','Rwanda'),('ET','Ethiopia'),('NG','Nigeria'),('GH','Ghana')],
    })


def login_view(request):
    if request.user.is_authenticated:
        if Wallet.objects.filter(wallet_user=request.user).exists():
            return redirect('dashboard')
        # Authenticated but no wallet (e.g. an interrupted/failed past
        # registration) — log them out instead of bouncing forever
        # between login and dashboard.
        auth_logout(request)
        messages.error(request, 'Your account has no wallet on file. Please register again or contact support.')

    locked_until = None
    error = False
    attempts_remaining = 5

    if request.method == 'POST':
        phone = request.POST.get('phone', '').strip()
        pin   = request.POST.get('pin', '')

        # Risk #03 & #08: IP-level rate limit — 10 attempts/15 min per IP
        ip = get_client_ip(request)
        allowed, _ = _check_rate_limit('login_ip', ip, 10, 900)
        if not allowed:
            messages.error(request, 'Too many login attempts from your network. Please try again in 15 minutes.')
            return render(request, 'wallet/login.html', {'locked_until': 'shortly'})

        try:
            user = WalletUser.objects.get(phone=phone)

            # Risk #03: account-level lockout
            if user.is_locked():
                locked_until = user.locked_until.strftime('%H:%M')
                return render(request, 'wallet/login.html', {
                    'locked_until': locked_until,
                    'error': False,
                })

            if user.check_pin(pin):
                user.record_successful_login()
                auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                return redirect('dashboard')
            else:
                user.record_failed_login()
                attempts_remaining = max(0, 5 - user.failed_login_attempts)
                error = True
                if user.is_locked():
                    locked_until = user.locked_until.strftime('%H:%M')
        except WalletUser.DoesNotExist:
            # Consistent timing to prevent user enumeration
            import time; time.sleep(0.3)
            error = True
            attempts_remaining = 5

    return render(request, 'wallet/login.html', {
        'error': error,
        'locked_until': locked_until,
        'attempts_remaining': attempts_remaining,
    })


def logout_view(request):
    auth_logout(request)
    return redirect('login')


# ─────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────

@wallet_required
def dashboard_view(request, wallet):
    ctx = _dashboard_context(wallet)
    return render(request, 'wallet/dashboard.html', ctx)


# ─────────────────────────────────────────────
# Deposit — M-Pesa STK Push
# ─────────────────────────────────────────────

@wallet_required
def mpesa_deposit_view(request, wallet):
    mock_mode = getattr(__import__('django.conf', fromlist=['settings']).settings, 'MPESA_CONFIG', {}).get('USE_MOCK', False)

    if request.method == 'POST' and request.POST.get('deposit_method') == 'mpesa':
        # Risk #08: rate limit deposits
        allowed, _ = _check_rate_limit('deposit', str(wallet.id), 10, 3600)
        if not allowed:
            messages.error(request, 'Too many deposit requests. Please wait before trying again.')
            return redirect('mpesa_deposit')

        phone  = request.POST.get('phone', wallet.phone).strip()
        amount_str = request.POST.get('amount', '').strip()
        try:
            amount = Decimal(amount_str)
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError):
            messages.error(request, 'Invalid amount.')
            return redirect('mpesa_deposit')

        idempotency_key = str(uuid.uuid4())
        try:
            client = MpesaClient()
            response = client.stk_push(phone=phone, amount=float(amount),
                                        account_ref=wallet.wallet_id,
                                        transaction_desc='KWallet Deposit')
            checkout_id = response.get('CheckoutRequestID', '')
            if checkout_id:
                MpesaTransaction.objects.create(
                    wallet=wallet,
                    checkout_request_id=checkout_id,
                    merchant_request_id=response.get('MerchantRequestID', ''),
                    amount=amount,
                    phone=phone,
                    status='pending',
                    transaction_type='mpesa_deposit',
                    # Risk #04: set timeout for orphaned transactions
                    timeout_at=timezone.now() + timezone.timedelta(minutes=30),
                )
                messages.success(request, f'M-Pesa prompt sent to {phone}. Enter your PIN to complete.')
            else:
                messages.error(request, 'Could not initiate M-Pesa request. Please try again.')
        except Exception as e:
            logger.exception('M-Pesa STK push failed')
            messages.error(request, 'M-Pesa service unavailable. Please try again.')

        return redirect('mpesa_deposit')

    return render(request, 'wallet/mpesa_deposit.html', {
        'wallet': wallet,
        'mock_mode': mock_mode,
        'home_currency': wallet.home_currency,
        'rates_stale': rates_are_stale(),
    })


# ─────────────────────────────────────────────
# Deposit — Airtel Money
# ─────────────────────────────────────────────

@wallet_required
def airtel_deposit_view(request, wallet):
    if request.method == 'POST':
        allowed, _ = _check_rate_limit('deposit', str(wallet.id), 10, 3600)
        if not allowed:
            messages.error(request, 'Too many deposit requests. Please wait.')
            return redirect('mpesa_deposit')

        phone = request.POST.get('phone', '').strip()
        amount_str = request.POST.get('amount', '').strip()
        try:
            amount = Decimal(amount_str)
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError):
            messages.error(request, 'Invalid amount.')
            return redirect('mpesa_deposit')

        client = AirtelClient()
        if not client.validate_ke_number(phone):
            messages.error(request, 'Please enter a valid Airtel Kenya number (073x, 075x, 078x).')
            return redirect('mpesa_deposit')

        idempotency_key = str(uuid.uuid4())
        try:
            response = client.collection_request(phone=phone, amount=float(amount),
                                                  ref=wallet.wallet_id,
                                                  idempotency_key=idempotency_key)
            airtel_ref = response.get('data', {}).get('transaction', {}).get('id', idempotency_key)
            AirtelTransaction.objects.create(
                wallet=wallet,
                airtel_ref=airtel_ref,
                amount=amount,
                phone=phone,
                status='pending',
                transaction_type='airtel_deposit',
                timeout_at=timezone.now() + timezone.timedelta(minutes=30),
            )
            messages.success(request, f'Airtel Money prompt sent to {phone}. Enter your Airtel PIN to complete.')
        except Exception:
            logger.exception('Airtel collection request failed')
            messages.error(request, 'Airtel Money service unavailable. Please try again.')

        return redirect('mpesa_deposit')

    return redirect('mpesa_deposit')


# ─────────────────────────────────────────────
# Deposit — Bank Transfer notification
# ─────────────────────────────────────────────

@wallet_required
def bank_deposit_notify_view(request, wallet):
    if request.method == 'POST':
        amount_str = request.POST.get('amount', '').strip()
        bank_name  = request.POST.get('bank_name', '').strip()
        bank_ref   = request.POST.get('bank_ref', '').strip()
        try:
            amount = Decimal(amount_str)
        except (InvalidOperation, TypeError):
            messages.error(request, 'Invalid amount.')
            return redirect('mpesa_deposit')

        BankTransaction.objects.create(
            wallet=wallet,
            pesalink_ref=bank_ref or str(uuid.uuid4()),
            amount=amount,
            bank_name=bank_name,
            account_number='',
            account_name='',
            status='pending',
            transaction_type='bank_deposit',
            timeout_at=timezone.now() + timezone.timedelta(hours=4),
        )
        Transaction.objects.create(
            wallet=wallet,
            transaction_type='bank_deposit',
            currency='KES',
            amount=amount,
            fee=0,
            status='pending',
            details=f'Bank deposit — {bank_name}, ref {bank_ref}',
            external_ref=bank_ref,
        )
        messages.success(request, 'Deposit notification received. Funds will be credited once confirmed by our team (usually within 2 hours).')
        return redirect('mpesa_deposit')

    return redirect('mpesa_deposit')


# ─────────────────────────────────────────────
# M-Pesa Callback (STK push result)
# ─────────────────────────────────────────────

@csrf_exempt
@require_POST
def mpesa_callback(request):
    """
    Risk #02: select_for_update prevents double-credit.
    Risk #05: IP allowlist + HMAC secret verified before processing.
    """
    # Risk #05: verify Safaricom IP
    client = MpesaClient()
    client_ip = get_client_ip(request)
    if not client.verify_callback_ip(client_ip):
        logger.warning(f'M-Pesa callback from unallowed IP: {client_ip}')
        return JsonResponse({'ResultCode': 1, 'ResultDesc': 'Unauthorised'}, status=403)

    # Risk #05: verify HMAC secret header
    secret = request.headers.get('X-Safaricom-Secret', '')
    if not client.verify_callback_secret(secret):
        logger.warning('M-Pesa callback HMAC secret mismatch')
        return JsonResponse({'ResultCode': 1, 'ResultDesc': 'Unauthorised'}, status=403)

    try:
        body = json.loads(request.body)
        stk = body['Body']['stkCallback']
        checkout_id = stk['CheckoutRequestID']
        result_code = stk['ResultCode']
    except (KeyError, json.JSONDecodeError) as e:
        logger.error(f'Malformed M-Pesa callback: {e}')
        return JsonResponse({'ResultCode': 1, 'ResultDesc': 'Bad payload'}, status=400)

    try:
        # Risk #02: atomic select_for_update — only one credit per checkout_id
        with db_transaction.atomic():
            mpesa_txn = MpesaTransaction.objects.select_for_update().get(
                checkout_request_id=checkout_id, status='pending'
            )
            if result_code == 0:
                # Extract M-Pesa receipt
                items = stk.get('CallbackMetadata', {}).get('Item', [])
                meta  = {i['Name']: i.get('Value') for i in items}
                receipt = meta.get('MpesaReceiptNumber', '')
                amount  = Decimal(str(meta.get('Amount', mpesa_txn.amount)))

                # Risk #05: validate amount matches what we requested
                if abs(amount - mpesa_txn.amount) > Decimal('0.01'):
                    logger.error(f'M-Pesa amount mismatch: expected {mpesa_txn.amount}, got {amount}')
                    mpesa_txn.status = 'failed'
                    mpesa_txn.save()
                    return JsonResponse({'ResultCode': 1, 'ResultDesc': 'Amount mismatch'})

                mpesa_txn.status = 'completed'
                mpesa_txn.mpesa_receipt = receipt
                mpesa_txn.save()

                _credit_balance(mpesa_txn.wallet, 'KES', amount, 'mpesa_deposit',
                                external_ref=receipt, fee=Decimal('0'))
                _pool_in(mpesa_txn.wallet, 'KES', amount, receipt)

            else:
                mpesa_txn.status = 'failed'
                mpesa_txn.save()

    except MpesaTransaction.DoesNotExist:
        # Risk #05: replay attack guard — already completed or unknown
        logger.warning(f'M-Pesa callback for unknown/completed checkout: {checkout_id}')

    return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@csrf_exempt
@require_POST
def mpesa_b2c_result(request):
    """B2C withdrawal result callback. Risk #04: resolve pending withdrawal."""
    client = MpesaClient()
    client_ip = get_client_ip(request)
    if not client.verify_callback_ip(client_ip):
        return JsonResponse({'ResultCode': 1, 'ResultDesc': 'Unauthorised'}, status=403)

    try:
        body = json.loads(request.body)
        result = body['Result']
        result_code = result['ResultCode']
        conversation_id = result.get('ConversationID', '')
    except (KeyError, json.JSONDecodeError):
        return JsonResponse({'ResultCode': 1, 'ResultDesc': 'Bad payload'}, status=400)

    try:
        with db_transaction.atomic():
            mpesa_txn = MpesaTransaction.objects.select_for_update().get(
                checkout_request_id=conversation_id, status='pending'
            )
            if result_code == 0:
                mpesa_txn.status = 'completed'
                mpesa_txn.save()
                # Update wallet transaction to completed
                Transaction.objects.filter(
                    wallet=mpesa_txn.wallet,
                    transaction_type='mpesa_withdraw',
                    external_ref=conversation_id,
                    status='pending',
                ).update(status='completed')
                _pool_out(mpesa_txn.wallet, 'KES', mpesa_txn.amount, conversation_id)
            else:
                # Risk #04: refund on B2C failure
                mpesa_txn.status = 'failed'
                mpesa_txn.save()
                _refund_balance(mpesa_txn.wallet, 'KES', mpesa_txn.amount, conversation_id)
    except MpesaTransaction.DoesNotExist:
        pass

    return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@csrf_exempt
@require_POST
def airtel_callback(request):
    """
    Airtel Money callback — collection (deposit) and disbursement (withdrawal).
    Risk #02: select_for_update prevents double-credit.
    Risk #05: shared-secret header verified.
    """
    client = AirtelClient()
    client_ip = get_client_ip(request)
    if not client.verify_callback_ip(client_ip):
        logger.warning(f'Airtel callback from unallowed IP: {client_ip}')
        return JsonResponse({'status': 'error'}, status=403)

    secret = request.headers.get('X-Airtel-Signature', '')
    if not client.verify_callback_secret(secret):
        logger.warning('Airtel callback secret mismatch')
        return JsonResponse({'status': 'error'}, status=403)

    try:
        body = json.loads(request.body)
        txn_data = body.get('transaction', {})
        airtel_ref   = txn_data.get('id', '')
        status_code  = txn_data.get('status_code', 'TS')
        amount       = Decimal(str(txn_data.get('amount', 0)))
    except (KeyError, json.JSONDecodeError, InvalidOperation):
        return JsonResponse({'status': 'error'}, status=400)

    try:
        with db_transaction.atomic():
            airtel_txn = AirtelTransaction.objects.select_for_update().get(
                airtel_ref=airtel_ref, status='pending'
            )
            if status_code == 'TS':  # Transaction Successful
                airtel_txn.status = 'completed'
                airtel_txn.save()
                if airtel_txn.transaction_type == 'airtel_deposit':
                    _credit_balance(airtel_txn.wallet, 'KES', amount, 'airtel_deposit',
                                    external_ref=airtel_ref, fee=Decimal('0'))
                    _pool_in(airtel_txn.wallet, 'KES', amount, airtel_ref)
                else:
                    Transaction.objects.filter(
                        wallet=airtel_txn.wallet,
                        transaction_type='airtel_withdraw',
                        external_ref=airtel_ref,
                        status='pending',
                    ).update(status='completed')
                    _pool_out(airtel_txn.wallet, 'KES', amount, airtel_ref)
            else:
                airtel_txn.status = 'failed'
                airtel_txn.save()
                if airtel_txn.transaction_type == 'airtel_withdraw':
                    _refund_balance(airtel_txn.wallet, 'KES', amount, airtel_ref)
    except AirtelTransaction.DoesNotExist:
        logger.warning(f'Airtel callback for unknown/completed ref: {airtel_ref}')

    return JsonResponse({'status': 'success'})


# ─────────────────────────────────────────────
# Withdraw — unified landing page
# ─────────────────────────────────────────────

@wallet_required
def withdraw_view(request, wallet):
    daily_withdrawn = float(wallet.get_daily_withdrawn())
    daily_pct       = wallet.get_daily_pct()
    kes_balance     = wallet.get_kes_balance()
    return render(request, 'wallet/withdraw.html', {
        'wallet':          wallet,
        'bank_choices':    BANK_CHOICES,
        'kes_balance':     kes_balance,
        'home_currency':   wallet.home_currency,
        'rates_stale':     rates_are_stale(),
        'daily_withdrawn': daily_withdrawn,
        'daily_limit':     DAILY_WITHDRAW_LIMIT,
        'daily_pct':       daily_pct,
    })


# ─────────────────────────────────────────────
# Withdraw — M-Pesa B2C
# ─────────────────────────────────────────────

@wallet_required
def mpesa_withdraw_view(request, wallet):
    if request.method != 'POST':
        return redirect('withdraw')

    # Risk #15: enforce KYC
    if wallet.kyc_status != 'verified':
        messages.error(request, 'Identity verification required before withdrawals.')
        return redirect('withdraw')

    # Risk #08: rate limit withdrawals
    allowed, _ = _check_rate_limit('withdraw', str(wallet.id), 10, 3600)
    if not allowed:
        messages.error(request, 'Too many withdrawal requests. Please try again later.')
        return redirect('withdraw')

    phone = request.POST.get('phone', wallet.phone).strip() or wallet.phone
    amount_str = request.POST.get('amount', '').strip()
    try:
        amount = Decimal(amount_str)
        if amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError):
        messages.error(request, 'Invalid amount.')
        return redirect('withdraw')

    fee   = Decimal(str(get_withdraw_fee(float(amount))))
    total = amount + fee

    # Risk #16: AML daily limit check
    daily_used = wallet.get_daily_withdrawn()
    if daily_used + float(total) > DAILY_WITHDRAW_LIMIT:
        messages.error(request, f'This would exceed your daily withdrawal limit of KES {DAILY_WITHDRAW_LIMIT:,}.')
        return redirect('withdraw')

    # Risk #16: AML velocity checks
    _check_aml_velocity(wallet, amount)

    # Balance check
    kes_balance = wallet.get_kes_balance()
    if total > Decimal(str(kes_balance)):
        messages.error(request, f'Insufficient balance. Available: KES {kes_balance:,.2f}')
        return redirect('withdraw')

    idempotency_key = str(uuid.uuid4())
    try:
        with db_transaction.atomic():
            # Debit balance atomically before initiating B2C
            _debit_balance(wallet, 'KES', total, 'mpesa_withdraw',
                           fee=fee, idempotency_key=idempotency_key)

            client = MpesaClient()
            response = client.b2c_payment(
                phone=phone, amount=float(amount),
                remarks=f'KWallet withdrawal {wallet.wallet_id}',
            )
            conversation_id = response.get('ConversationID', idempotency_key)

            MpesaTransaction.objects.create(
                wallet=wallet,
                checkout_request_id=conversation_id,
                amount=amount,
                phone=phone,
                status='pending',
                transaction_type='mpesa_withdraw',
                # Risk #04: auto-timeout for orphaned withdrawal
                timeout_at=timezone.now() + timezone.timedelta(minutes=30),
            )
        messages.success(request, f'Withdrawal of KES {amount:,.2f} is being processed to {phone}.')
    except Exception:
        logger.exception('M-Pesa B2C withdraw failed')
        # Risk #04: reverse debit if B2C initiation failed
        try:
            _refund_balance(wallet, 'KES', total, idempotency_key)
        except Exception:
            logger.exception('Refund also failed — manual reconciliation needed')
        messages.error(request, 'Withdrawal failed. Your balance has been restored. Please try again.')

    return redirect('withdraw')


# ─────────────────────────────────────────────
# Withdraw — Airtel Money
# ─────────────────────────────────────────────

@wallet_required
def airtel_withdraw_view(request, wallet):
    if request.method != 'POST':
        return redirect('withdraw')

    if wallet.kyc_status != 'verified':
        messages.error(request, 'Identity verification required before withdrawals.')
        return redirect('withdraw')

    allowed, _ = _check_rate_limit('withdraw', str(wallet.id), 10, 3600)
    if not allowed:
        messages.error(request, 'Too many withdrawal requests.')
        return redirect('withdraw')

    phone = request.POST.get('phone', '').strip()
    amount_str = request.POST.get('amount', '').strip()
    try:
        amount = Decimal(amount_str)
        if amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError):
        messages.error(request, 'Invalid amount.')
        return redirect('withdraw')

    airtel_client = AirtelClient()
    if not airtel_client.validate_ke_number(phone):
        messages.error(request, 'Invalid Airtel Kenya number. Supported prefixes: 073x, 075x, 078x.')
        return redirect('withdraw')

    fee   = Decimal(str(get_withdraw_fee(float(amount))))
    total = amount + fee

    daily_used = wallet.get_daily_withdrawn()
    if daily_used + float(total) > DAILY_WITHDRAW_LIMIT:
        messages.error(request, f'Exceeds daily limit of KES {DAILY_WITHDRAW_LIMIT:,}.')
        return redirect('withdraw')

    _check_aml_velocity(wallet, amount)

    kes_balance = wallet.get_kes_balance()
    if total > Decimal(str(kes_balance)):
        messages.error(request, f'Insufficient balance. Available: KES {kes_balance:,.2f}')
        return redirect('withdraw')

    idempotency_key = str(uuid.uuid4())
    try:
        with db_transaction.atomic():
            _debit_balance(wallet, 'KES', total, 'airtel_withdraw',
                           fee=fee, idempotency_key=idempotency_key)
            response = airtel_client.disbursement_request(
                phone=phone, amount=float(amount),
                ref=wallet.wallet_id, idempotency_key=idempotency_key,
            )
            airtel_ref = response.get('data', {}).get('transaction', {}).get('id', idempotency_key)
            AirtelTransaction.objects.create(
                wallet=wallet, airtel_ref=airtel_ref, amount=amount, phone=phone,
                status='pending', transaction_type='airtel_withdraw',
                timeout_at=timezone.now() + timezone.timedelta(minutes=30),
            )
        messages.success(request, f'Airtel Money withdrawal of KES {amount:,.2f} is being processed to {phone}.')
    except Exception:
        logger.exception('Airtel disbursement failed')
        try:
            _refund_balance(wallet, 'KES', total, idempotency_key)
        except Exception:
            logger.exception('Refund also failed')
        messages.error(request, 'Withdrawal failed. Your balance has been restored.')

    return redirect('withdraw')


# ─────────────────────────────────────────────
# Withdraw — Bank Transfer
# ─────────────────────────────────────────────

@wallet_required
def bank_withdraw_view(request, wallet):
    if request.method != 'POST':
        return redirect('withdraw')

    if wallet.kyc_status != 'verified':
        messages.error(request, 'Identity verification required.')
        return redirect('withdraw')

    allowed, _ = _check_rate_limit('withdraw', str(wallet.id), 5, 3600)
    if not allowed:
        messages.error(request, 'Too many withdrawal requests.')
        return redirect('withdraw')

    bank_name      = request.POST.get('bank_name', '').strip()
    account_number = request.POST.get('account_number', '').strip()
    account_name   = request.POST.get('account_name', '').strip()
    amount_str     = request.POST.get('amount', '').strip()

    if not all([bank_name, account_number, account_name]):
        messages.error(request, 'Bank name, account number and account name are all required.')
        return redirect('withdraw')

    try:
        amount = Decimal(amount_str)
        if amount < 500:
            raise InvalidOperation
    except (InvalidOperation, TypeError):
        messages.error(request, 'Minimum bank transfer amount is KES 500.')
        return redirect('withdraw')

    fee   = Decimal(str(BANK_WITHDRAW_FEE))
    total = amount + fee

    daily_used = wallet.get_daily_withdrawn()
    if daily_used + float(total) > DAILY_WITHDRAW_LIMIT:
        messages.error(request, f'Exceeds daily limit of KES {DAILY_WITHDRAW_LIMIT:,}.')
        return redirect('withdraw')

    _check_aml_velocity(wallet, amount)

    # Risk #16: enhanced verification for large bank transfers
    if amount > Decimal('100000'):
        messages.warning(request, 'Transfers above KES 100,000 require enhanced verification. Our compliance team will contact you within 1 business day.')

    kes_balance = wallet.get_kes_balance()
    if total > Decimal(str(kes_balance)):
        messages.error(request, f'Insufficient balance. Available: KES {kes_balance:,.2f}')
        return redirect('withdraw')

    pesalink_ref = 'PL' + uuid.uuid4().hex[:12].upper()
    try:
        with db_transaction.atomic():
            _debit_balance(wallet, 'KES', total, 'bank_withdraw',
                           fee=fee, idempotency_key=pesalink_ref,
                           bank_name=bank_name, bank_account=account_number)
            BankTransaction.objects.create(
                wallet=wallet,
                pesalink_ref=pesalink_ref,
                amount=amount,
                bank_name=bank_name,
                account_number=account_number,
                account_name=account_name,
                status='pending',
                transaction_type='bank_withdraw',
                timeout_at=timezone.now() + timezone.timedelta(hours=4),
            )
        messages.success(request, f'Bank transfer of KES {amount:,.2f} to {bank_name} is being processed. Reference: {pesalink_ref}')
    except Exception:
        logger.exception('Bank withdraw failed')
        try:
            _refund_balance(wallet, 'KES', total, pesalink_ref)
        except Exception:
            logger.exception('Refund also failed')
        messages.error(request, 'Withdrawal failed. Your balance has been restored.')

    return redirect('withdraw')


# ─────────────────────────────────────────────
# STK Query (manual poll)
# ─────────────────────────────────────────────

@wallet_required
def stk_query_view(request, wallet):
    """Risk #02: STK query only reads status — never credits independently."""
    checkout_id = request.GET.get('checkout_id', '')
    if not checkout_id:
        return JsonResponse({'status': 'error', 'message': 'Missing checkout_id'}, status=400)

    try:
        mpesa_txn = MpesaTransaction.objects.get(
            checkout_request_id=checkout_id, wallet=wallet
        )
    except MpesaTransaction.DoesNotExist:
        return JsonResponse({'status': 'not_found'}, status=404)

    # Risk #02: only report status — callback is sole authority for crediting
    return JsonResponse({'status': mpesa_txn.status, 'amount': str(mpesa_txn.amount)})


# ─────────────────────────────────────────────
# Exchange
# ─────────────────────────────────────────────

@wallet_required
def exchange_view(request, wallet):
    balances = wallet.currency_balances.all().order_by('currency')
    stale    = rates_are_stale()

    if request.method == 'POST':
        if wallet.kyc_status != 'verified':
            messages.error(request, 'KYC required for currency exchange.')
            return redirect('exchange')

        from_curr = request.POST.get('from_currency', '').strip()
        to_curr   = request.POST.get('to_currency', '').strip()
        amount_str = request.POST.get('amount', '').strip()

        if from_curr == to_curr:
            messages.error(request, 'From and To currencies must be different.')
            return redirect('exchange')

        try:
            amount = Decimal(amount_str)
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError):
            messages.error(request, 'Invalid amount.')
            return redirect('exchange')

        # Risk #01: block large exchanges on stale rates
        if stale:
            try:
                rates = get_rates()
                kes_rate = float(rates.get(f'{from_curr}_KES', 1))
                kes_equiv = float(amount) * kes_rate
            except Exception:
                kes_equiv = float(amount)
            if kes_equiv > STALE_RATE_MAX_EXCHANGE:
                messages.error(request, f'Exchange paused: live rates unavailable. Limit KES {STALE_RATE_MAX_EXCHANGE:,} while on fallback rates.')
                return redirect('exchange')

        try:
            rate = Decimal(str(get_pair_rate(from_curr, to_curr)))
        except Exception:
            messages.error(request, 'Exchange rate unavailable. Please try again.')
            return redirect('exchange')

        # Fee tiers
        usd_rate = Decimal(str(get_pair_rate(from_curr, 'USD') or 1/130))
        usd_equiv = amount * usd_rate
        if usd_equiv > 10000:
            fee_rate = Decimal('0.005')
        elif usd_equiv > 2000:
            fee_rate = Decimal('0.0075')
        elif usd_equiv > 500:
            fee_rate = Decimal('0.010')
        else:
            fee_rate = Decimal('0.015')
        fee = (amount * fee_rate).quantize(Decimal('0.000001'))
        net_amount = amount - fee
        converted  = (net_amount * rate).quantize(Decimal('0.000001'))

        try:
            from_cb = wallet.currency_balances.get(currency=from_curr)
            to_cb   = wallet.currency_balances.get(currency=to_curr)
        except CurrencyBalance.DoesNotExist:
            messages.error(request, 'Currency not found in your wallet.')
            return redirect('exchange')

        if from_cb.balance < amount:
            messages.error(request, f'Insufficient {from_curr} balance.')
            return redirect('exchange')

        idempotency_key = str(uuid.uuid4())
        with db_transaction.atomic():
            from_cb = CurrencyBalance.objects.select_for_update().get(pk=from_cb.pk)
            to_cb   = CurrencyBalance.objects.select_for_update().get(pk=to_cb.pk)
            from_cb.balance -= amount
            to_cb.balance   += converted
            from_cb.save()
            to_cb.save()
            Transaction.objects.create(
                wallet=wallet,
                transaction_type='exchange',
                currency=from_curr,
                amount=amount,
                fee=fee,
                status='completed',
                details=f'{from_curr} → {to_curr} @ {rate:.6f}',
                idempotency_key=idempotency_key,
            )

        messages.success(request, f'Exchanged {from_curr} {amount:,.4f} → {to_curr} {converted:,.4f}')
        return redirect('dashboard')

    return render(request, 'wallet/exchange.html', {
        'wallet': wallet,
        'balances': balances,
        'home_currency': wallet.home_currency,
        'rates_stale': stale,
    })


# ─────────────────────────────────────────────
# P2P Transfer
# ─────────────────────────────────────────────

@wallet_required
def p2p_view(request, wallet):
    balances = wallet.currency_balances.all().order_by('currency')

    if request.method == 'POST':
        if wallet.kyc_status != 'verified':
            messages.error(request, 'KYC required for transfers.')
            return redirect('p2p')

        allowed, _ = _check_rate_limit('p2p', str(wallet.id), 20, 3600)
        if not allowed:
            messages.error(request, 'Too many transfer requests.')
            return redirect('p2p')

        recipient_phone = request.POST.get('recipient_phone', '').strip()
        currency        = request.POST.get('currency', 'KES')
        amount_str      = request.POST.get('amount', '').strip()
        note            = request.POST.get('note', '')[:80]

        try:
            amount = Decimal(amount_str)
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError):
            messages.error(request, 'Invalid amount.')
            return redirect('p2p')

        try:
            recipient_user   = WalletUser.objects.get(phone=recipient_phone)
            recipient_wallet = recipient_user.wallet  # via wallet_user FK
        except (WalletUser.DoesNotExist, Wallet.DoesNotExist, AttributeError):
            messages.error(request, 'Recipient not found. Please check the phone number.')
            return redirect('p2p')

        if recipient_wallet == wallet:
            messages.error(request, 'You cannot send to yourself.')
            return redirect('p2p')

        fee = Decimal(str(get_send_fee(float(amount)))) if currency == 'KES' else Decimal('0')
        total = amount + fee

        _check_aml_velocity(wallet, amount if currency == 'KES' else Decimal('0'))

        try:
            from_cb = wallet.currency_balances.get(currency=currency)
        except CurrencyBalance.DoesNotExist:
            messages.error(request, f'{currency} not in your wallet.')
            return redirect('p2p')

        if from_cb.balance < total:
            messages.error(request, f'Insufficient {currency} balance.')
            return redirect('p2p')

        idempotency_key = str(uuid.uuid4())
        # Risk #06: only store masked recipient in details
        masked = mask_phone(recipient_phone)

        with db_transaction.atomic():
            from_cb_locked = CurrencyBalance.objects.select_for_update().get(pk=from_cb.pk)
            if from_cb_locked.balance < total:
                messages.error(request, 'Insufficient balance.')
                return redirect('p2p')
            from_cb_locked.balance -= total
            from_cb_locked.save()

            to_cb, _ = CurrencyBalance.objects.get_or_create(
                wallet=recipient_wallet, currency=currency,
                defaults={'balance': Decimal('0')}
            )
            to_cb_locked = CurrencyBalance.objects.select_for_update().get(pk=to_cb.pk)
            to_cb_locked.balance += amount
            to_cb_locked.save()

            # Risk #06: masked details only
            Transaction.objects.create(
                wallet=wallet, transaction_type='p2p_send',
                currency=currency, amount=amount, fee=fee,
                status='completed',
                details=f'Sent to {masked}',  # masked — not full name/phone
                recipient_wallet=recipient_wallet,
                idempotency_key=idempotency_key,
            )
            Transaction.objects.create(
                wallet=recipient_wallet, transaction_type='p2p_receive',
                currency=currency, amount=amount, fee=Decimal('0'),
                status='completed',
                details=f'Received from {mask_phone(wallet.phone)}',
                recipient_wallet=wallet,
            )

        messages.success(request, f'Sent {currency} {amount:,.2f} to {masked}.')
        return redirect('dashboard')

    return render(request, 'wallet/p2p.html', {
        'wallet': wallet,
        'balances': balances,
        'home_currency': wallet.home_currency,
        'daily_withdrawn': float(wallet.get_daily_withdrawn()),
        'daily_limit': DAILY_WITHDRAW_LIMIT,
        'daily_pct': wallet.get_daily_pct(),
    })


# ─────────────────────────────────────────────
# Currencies
# ─────────────────────────────────────────────

@wallet_required
def add_currency_view(request, wallet):
    active     = list(wallet.currency_balances.values_list('currency', flat=True))
    # Risk #14: enforce max currencies
    if request.method == 'POST':
        if len(active) >= MAX_CURRENCIES:
            messages.error(request, f'Maximum of {MAX_CURRENCIES} currencies allowed.')
            return redirect('add_currency')
        currency = request.POST.get('currency', '').strip().upper()
        all_valid = [c for c, _ in EA_CURRENCIES + INTL_CURRENCIES]
        if currency not in all_valid:
            messages.error(request, 'Invalid currency.')
            return redirect('add_currency')
        if currency in active:
            messages.error(request, f'{currency} is already in your wallet.')
            return redirect('add_currency')
        CurrencyBalance.objects.create(wallet=wallet, currency=currency, balance=0)
        messages.success(request, f'{currency} added to your wallet.')
        return redirect('dashboard')

    return render(request, 'wallet/add_currency.html', {
        'wallet': wallet,
        'active_currencies': active,
        'active_count': len(active),
        'max_currencies': MAX_CURRENCIES,
        'ea_currencies': EA_CURRENCIES,
        'intl_currencies': INTL_CURRENCIES,
    })


# ─────────────────────────────────────────────
# Transaction History
# ─────────────────────────────────────────────

@wallet_required
def transactions_view(request, wallet):
    qs = wallet.transactions.all()
    txn_type = request.GET.get('type', '')
    status   = request.GET.get('status', '')
    currency = request.GET.get('currency', '')
    if txn_type:
        qs = qs.filter(transaction_type=txn_type)
    if status:
        qs = qs.filter(status=status)
    if currency:
        qs = qs.filter(currency=currency)

    paginator = Paginator(qs, 20)
    page      = paginator.get_page(request.GET.get('page', 1))
    available_currencies = list(
        wallet.currency_balances.values_list('currency', flat=True)
    )
    return render(request, 'wallet/transactions.html', {
        'wallet': wallet,
        'transactions': page,
        'available_currencies': available_currencies,
    })


# ─────────────────────────────────────────────
# Rates API (Risk #09: no config leak)
# ─────────────────────────────────────────────

@wallet_required
def rates_api_view(request, wallet):
    """Risk #09: authenticated endpoint — no environment/config leak."""
    try:
        rates = get_rates()
        return JsonResponse(rates)
    except Exception:
        return JsonResponse({}, status=503)


# ─────────────────────────────────────────────
# Health check (Risk #09: no config leak)
# ─────────────────────────────────────────────

def health_check(request):
    """Risk #09: return only OK/error — never environment or mock_mode."""
    try:
        from django.db import connection
        connection.ensure_connection()
        db_ok = True
    except Exception:
        db_ok = False
    status = 200 if db_ok else 503
    # Risk #09: NO environment, NO mock_mode, NO config details
    return JsonResponse({'status': 'ok' if db_ok else 'error', 'database': 'ok' if db_ok else 'error'}, status=status)


# ─────────────────────────────────────────────
# QR Payment views
# ─────────────────────────────────────────────

@wallet_required
def qr_payment_list(request, wallet):
    reqs = wallet.qr_requests.all().order_by('-created_at')
    return render(request, 'wallet/qr_list.html', {'wallet': wallet, 'payment_requests': reqs})


@wallet_required
def qr_payment_create(request, wallet):
    if request.method == 'POST':
        amount_str  = request.POST.get('amount', '').strip()
        note        = request.POST.get('note', '')[:120]
        single_use  = bool(request.POST.get('single_use'))
        expires_raw = request.POST.get('expires_at', '').strip()

        amount = None
        if amount_str:
            try:
                amount = Decimal(amount_str)
            except InvalidOperation:
                messages.error(request, 'Invalid amount.')
                return redirect('qr_payment_create')

        expires_at = None
        if expires_raw:
            try:
                from django.utils.dateparse import parse_datetime
                expires_at = parse_datetime(expires_raw)
            except Exception:
                pass

        token = uuid.uuid4().hex
        QRPaymentRequest.objects.create(
            wallet=wallet, token=token, amount=amount, note=note,
            single_use=single_use, expires_at=expires_at, status='active',
        )
        return redirect('qr_payment_detail', token=token)

    return render(request, 'wallet/qr_create.html', {'wallet': wallet})


def qr_payment_detail(request, token):
    req = get_object_or_404(QRPaymentRequest, token=token)
    if not request.user.is_authenticated or request.user.wallet != req.wallet:
        return redirect('login')

    import qrcode, io, base64
    pay_url = request.build_absolute_uri(f'/pay/{token}/')
    qr = qrcode.make(pay_url)
    buf = io.BytesIO()
    qr.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    qr_svg = f'<img src="data:image/png;base64,{qr_b64}" width="180" height="180" alt="QR Code">'

    return render(request, 'wallet/qr_detail.html', {
        'wallet': req.wallet, 'req': req, 'qr_svg': qr_svg, 'pay_url': pay_url,
    })


@wallet_required
def qr_payment_disable(request, wallet, token):
    req = get_object_or_404(QRPaymentRequest, token=token, wallet=wallet)
    req.status = 'disabled'
    req.save()
    messages.success(request, 'Payment link disabled.')
    return redirect('qr_payment_list')


def qr_pay_view(request, token):
    req = get_object_or_404(QRPaymentRequest, token=token)
    if not req.is_valid():
        return render(request, 'wallet/qr_expired.html')

    if request.method == 'POST':
        phone  = request.POST.get('phone', '').strip()
        amount = req.amount or Decimal(request.POST.get('amount', '0'))
        try:
            amount = Decimal(str(amount))
        except InvalidOperation:
            messages.error(request, 'Invalid amount.')
            return render(request, 'wallet/qr_pay.html', {'req': req, 'token': token})

        reference = 'QR' + uuid.uuid4().hex[:10].upper()
        try:
            client = MpesaClient()
            client.stk_push(phone=phone, amount=float(amount),
                            account_ref=req.wallet.wallet_id,
                            transaction_desc=f'KWallet QR {req.note or token[:8]}')
            if req.single_use:
                req.status = 'paid'
                req.save()
            return render(request, 'wallet/qr_pay_pending.html', {
                'phone': phone, 'amount': amount,
                'reference': reference, 'token': token,
            })
        except Exception:
            messages.error(request, 'Payment initiation failed. Please try again.')

    return render(request, 'wallet/qr_pay.html', {'req': req, 'token': token})


# ─────────────────────────────────────────────
# KYC placeholder
# ─────────────────────────────────────────────

@wallet_required
def kyc_start_view(request, wallet):
    """Risk #15: KYC entry point. In production: integrate Smile Identity / Onfido."""
    if request.method == 'POST':
        # Placeholder: mark as pending review
        messages.info(request, 'KYC submission received. Verification usually takes less than 5 minutes.')
        return redirect('dashboard')
    return render(request, 'wallet/kyc_start.html', {'wallet': wallet})


# ─────────────────────────────────────────────
# PIN Reset
# ─────────────────────────────────────────────

def pin_reset_request_view(request):
    if request.method == 'POST':
        phone = request.POST.get('phone', '').strip()
        # Risk #03: rate limit PIN reset attempts
        allowed, _ = _check_rate_limit('pin_reset', phone, 3, 3600)
        if not allowed:
            messages.error(request, 'Too many reset requests. Please wait an hour.')
            return redirect('pin_reset_request')
        try:
            user = WalletUser.objects.get(phone=phone)
            code  = str(uuid.uuid4().int)[:6]
            token = uuid.uuid4().hex
            PinResetToken.objects.create(
                user=user, token=token, code=code,
                expires_at=timezone.now() + timezone.timedelta(minutes=15),
            )
            # In production: send SMS via Africa's Talking / Twilio
            logger.info(f'PIN reset code for {phone}: {code}')
            messages.success(request, f'Verification code sent to {phone}.')
            return redirect('pin_reset_verify')
        except WalletUser.DoesNotExist:
            # Consistent timing
            import time; time.sleep(0.3)
            messages.success(request, f'If {phone} is registered, a code has been sent.')
            return redirect('pin_reset_verify')
    return render(request, 'wallet/pin_reset_request.html')


def pin_reset_verify_view(request):
    if request.method == 'POST':
        phone = request.POST.get('phone', '').strip()
        code  = request.POST.get('code', '').strip()
        try:
            user  = WalletUser.objects.get(phone=phone)
            reset = PinResetToken.objects.filter(user=user, code=code, used=False).latest('created_at')
            if not reset.is_valid():
                return render(request, 'wallet/pin_reset_verify.html', {
                    'phone': phone, 'error': 'Code expired. Please request a new one.',
                })
            return redirect(f'/reset-pin/set/?phone={phone}&token={reset.token}')
        except (WalletUser.DoesNotExist, PinResetToken.DoesNotExist):
            return render(request, 'wallet/pin_reset_verify.html', {
                'phone': phone, 'error': 'Invalid code. Please try again.',
            })
    return render(request, 'wallet/pin_reset_verify.html', {
        'phone': request.GET.get('phone', ''),
    })


def pin_reset_set_view(request):
    phone = request.GET.get('phone') or request.POST.get('phone', '')
    token = request.GET.get('token') or request.POST.get('token', '')
    if request.method == 'POST':
        pin         = request.POST.get('pin', '')
        pin_confirm = request.POST.get('pin_confirm', '')
        if len(pin) < 6:
            return render(request, 'wallet/pin_reset_set.html', {
                'phone': phone, 'token': token, 'error': 'PIN must be at least 6 digits.',
            })
        if pin != pin_confirm:
            return render(request, 'wallet/pin_reset_set.html', {
                'phone': phone, 'token': token, 'error': 'PINs do not match.',
            })
        try:
            user  = WalletUser.objects.get(phone=phone)
            reset = PinResetToken.objects.get(user=user, token=token, used=False)
            if not reset.is_valid():
                messages.error(request, 'Reset link expired.')
                return redirect('pin_reset_request')
            user.set_pin(pin)
            user.save()
            reset.used = True
            reset.save()
            messages.success(request, 'PIN updated successfully. Please sign in.')
            return redirect('login')
        except (WalletUser.DoesNotExist, PinResetToken.DoesNotExist):
            messages.error(request, 'Invalid reset link.')
            return redirect('pin_reset_request')
    return render(request, 'wallet/pin_reset_set.html', {'phone': phone, 'token': token})


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _credit_balance(wallet, currency, amount, txn_type, external_ref='', fee=Decimal('0')):
    """Atomically credit a wallet balance and write a Transaction record."""
    with db_transaction.atomic():
        cb, _ = CurrencyBalance.objects.select_for_update().get_or_create(
            wallet=wallet, currency=currency, defaults={'balance': Decimal('0')}
        )
        cb.balance += amount
        cb.save()
        Transaction.objects.create(
            wallet=wallet, transaction_type=txn_type,
            currency=currency, amount=amount, fee=fee,
            status='completed', external_ref=external_ref,
        )


def _debit_balance(wallet, currency, amount, txn_type, fee=Decimal('0'),
                   idempotency_key=None, bank_name='', bank_account=''):
    """Atomically debit a wallet balance and write a pending Transaction."""
    with db_transaction.atomic():
        cb = CurrencyBalance.objects.select_for_update().get(wallet=wallet, currency=currency)
        if cb.balance < amount:
            raise ValueError('Insufficient balance')
        cb.balance -= amount
        cb.save()
        Transaction.objects.create(
            wallet=wallet, transaction_type=txn_type,
            currency=currency, amount=amount - fee, fee=fee,
            status='pending', idempotency_key=idempotency_key,
            bank_name=bank_name, bank_account=bank_account,
        )


def _refund_balance(wallet, currency, amount, ref=''):
    """Reverse a failed debit — used for Risk #04 orphaned transactions."""
    with db_transaction.atomic():
        cb, _ = CurrencyBalance.objects.select_for_update().get_or_create(
            wallet=wallet, currency=currency, defaults={'balance': Decimal('0')}
        )
        cb.balance += amount
        cb.save()
        Transaction.objects.create(
            wallet=wallet, transaction_type='refund',
            currency=currency, amount=amount, fee=Decimal('0'),
            status='completed', external_ref=ref,
            details='Auto-refund: disbursement failed or timed out',
        )


def _pool_in(wallet, currency, amount, ref=''):
    PoolLedger.objects.create(currency=currency, entry_type='deposit_in', amount=amount, reference=ref)
    acc, _ = CompanyAccount.objects.get_or_create(currency=currency, defaults={'balance': Decimal('0')})
    acc.balance += amount
    acc.save()


def _pool_out(wallet, currency, amount, ref=''):
    PoolLedger.objects.create(currency=currency, entry_type='withdrawal_out', amount=amount, reference=ref)
    acc, _ = CompanyAccount.objects.get_or_create(currency=currency, defaults={'balance': Decimal('0')})
    acc.balance -= amount
    acc.save()
    # Risk #12: alert on insolvency
    if acc.balance < 0:
        logger.critical(f'INSOLVENCY ALERT: CompanyAccount {currency} balance is {acc.balance}. Immediate action required.')
        try:
            from django.core.mail import mail_admins
            mail_admins(
                subject=f'[CRITICAL] KWallet {currency} pool INSOLVENT',
                message=f'Pool balance for {currency} has gone negative: {acc.balance}.\nReference: {ref}',
            )
        except Exception:
            logger.exception('Failed to send insolvency alert email')
