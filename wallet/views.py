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
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .flutterwave import FlutterwaveClient, CHANNEL_CARD, CHANNEL_BANK_TRANSFER, CHANNEL_MPESA, CHANNEL_AIRTEL
from .models import (
    BankTransaction, CompanyAccount, CurrencyBalance,
    FlutterwaveTransaction,
    PoolLedger, QRPaymentRequest, SuspiciousActivityFlag,
    Transaction, Wallet, WalletLimit, WalletUser, PinResetToken,
    MAX_CURRENCIES, STALE_RATE_MAX_EXCHANGE,
    get_send_fee, mask_phone, mask_name,
    LIMIT_TIERS,
)
from .rates import get_rates, get_pair_rate, rates_are_stale
from . import sandbox as _sandbox
from django.conf import settings as _settings

SANDBOX_MODE = getattr(_settings, 'WALLET_SANDBOX_MODE', True)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Helpers / Decorators
# ─────────────────────────────────────────────

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
    """Block view if KYC is not verified; sandbox wallets are exempt."""
    @wraps(view_fn)
    def wrapper(request, wallet, *args, **kwargs):
        if wallet.kyc_status != 'verified' and not _sandbox.is_sandbox(wallet):
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
    home_currency = wallet.home_currency or (balances.first().currency if balances.exists() else 'KES')
    total_value = sum(float(cb.balance) for cb in balances if cb.currency == home_currency)
    limits = wallet.get_effective_limits()
    tier   = wallet.get_limit_tier()
    return {
        'wallet': wallet,
        'balances': balances,
        'home_currency': home_currency,
        'recent_txns': recent_txns,
        'total_value': total_value,
        'rates_stale': rates_are_stale(),
        'daily_withdrawn': daily_withdrawn,
        'daily_limit': limits['daily'],
        'monthly_limit': limits['monthly'],
        'per_txn_limit': limits['per_txn'],
        'daily_pct': daily_pct,
        'limit_tier': tier,
        'limit_tier_label': LIMIT_TIERS[tier]['label'],
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
                from django.conf import settings as _dj_settings
                wallet = Wallet.objects.create(
                    wallet_id=wallet_id,
                    wallet_user=user,
                    wallet_id_str=wallet_id,
                    phone=phone,
                    home_currency='',  # No default — user selects on first login
                    kyc_status='pending',
                    country=country,
                    is_sandbox=getattr(_dj_settings, 'WALLET_SANDBOX_MODE', True),
                )
                WalletLimit.objects.create(wallet=wallet)

            auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            user.register_session(request.session.session_key)
            messages.success(request, f'Welcome, {first_name}! Please add at least one currency to your wallet, then complete KYC.')
            return redirect('add_currency')
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
        auth_logout(request)
        messages.error(request, 'Your account has no wallet on file. Please register again or contact support.')

    # Surface middleware-injected messages from previous request
    idle_msg   = request.session.pop('idle_timeout_msg', None)
    device_msg = request.session.pop('device_kick_msg', None)
    if idle_msg:
        messages.warning(request, idle_msg)
    if device_msg:
        messages.error(request, device_msg)

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
                # Rotate session to prevent fixation (clears old session data)
                request.session.cycle_key()
                auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                # ── Single-device enforcement: register this session ──────────
                # register_session must be called AFTER auth_login so that
                # request.session.session_key is finalised.
                user.register_session(request.session.session_key)
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
    if request.user.is_authenticated:
        try:
            # Clear the single-device session token so re-login works cleanly
            request.user.active_session_key = ''
            request.user.save(update_fields=['active_session_key'])
        except Exception:
            pass
    auth_logout(request)
    return redirect('login')


@require_POST
@login_required
def idle_ping_view(request):
    """
    Lightweight heartbeat endpoint called by the JS idle timer.
    Updates last_activity so the server-side idle check stays in sync.
    """
    try:
        request.user.touch_activity()
    except Exception:
        logger.exception('idle_ping touch_activity failed')
    return JsonResponse({'ok': True})


# ─────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────

@wallet_required
def dashboard_view(request, wallet):
    ctx = _dashboard_context(wallet)
    return render(request, 'wallet/dashboard.html', ctx)



# ─────────────────────────────────────────────
# Flutterwave — Card / Bank Transfer / Mobile deposit
# ─────────────────────────────────────────────

@wallet_required
def flw_deposit_view(request, wallet):
    """
    Initiate a Flutterwave payment (card, bank transfer, or mobile money).
    POST creates a hosted payment link and redirects the user to Flutterwave.
    GET renders the deposit form with channel selection.
    """
    if request.method == 'POST':
        # Risk #08: rate limit
        allowed, _ = _check_rate_limit('flw_deposit', str(wallet.wallet_id), 10, 3600)
        if not allowed:
            messages.error(request, 'Too many deposit requests. Please wait before trying again.')
            return redirect('flw_deposit')

        amount_str = request.POST.get('amount', '').strip()
        currency   = request.POST.get('currency', 'KES').strip().upper()
        channel    = request.POST.get('channel', CHANNEL_CARD).strip()

        try:
            amount = Decimal(amount_str)
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError):
            messages.error(request, 'Invalid amount.')
            return redirect('flw_deposit')

        # Risk #02: idempotency key
        tx_ref = f'kwallet_{wallet.wallet_id}_{uuid.uuid4().hex[:12]}'

        if _sandbox.is_sandbox(wallet):
            # Sandbox: directly credit and record
            _credit_balance(wallet, currency, amount, 'flw_card_deposit', external_ref=tx_ref)
            FlutterwaveTransaction.objects.create(
                wallet=wallet, tx_ref=tx_ref, flw_tx_id='sandbox',
                channel=channel, amount=amount, currency=currency,
                direction='in', status='completed',
                raw_payload={'sandbox': True},
                timeout_at=timezone.now() + timezone.timedelta(minutes=30),
            )
            messages.success(request, f'🧪 [Sandbox] Credited {currency} {amount:,.2f} via Flutterwave ({channel}).')
            return redirect('dashboard')

        try:
            client = FlutterwaveClient()

            # Mobile money: direct STK push
            if channel in (CHANNEL_MPESA, CHANNEL_AIRTEL):
                phone = request.POST.get('phone', wallet.phone).strip()
                network = 'MPESA' if channel == CHANNEL_MPESA else 'AIRTEL'
                result = client.initiate_mobile_money(
                    phone=phone, amount=float(amount), currency=currency,
                    tx_ref=tx_ref, network=network,
                    email=wallet.user.email if hasattr(wallet, 'user') and wallet.user.email else 'noreply@kwallet.ke',
                )
                FlutterwaveTransaction.objects.create(
                    wallet=wallet, tx_ref=tx_ref,
                    channel=channel, amount=amount, currency=currency,
                    phone=phone, direction='in', status='pending',
                    raw_payload=result,
                    timeout_at=timezone.now() + timezone.timedelta(minutes=30),
                )
                messages.success(request, f'Payment prompt sent to {phone}. Enter your PIN to complete.')
                return redirect('flw_deposit')

            # Card / bank transfer: hosted payment link
            user_email = getattr(getattr(wallet, 'user', None), 'email', '') or 'noreply@kwallet.ke'
            user_name  = getattr(getattr(wallet, 'user', None), 'get_full_name', lambda: '')() or wallet.phone

            result = client.create_payment_link(
                amount=float(amount),
                currency=currency,
                customer_email=user_email,
                customer_name=user_name,
                customer_phone=wallet.phone,
                tx_ref=tx_ref,
                description=f'KWallet deposit — {wallet.wallet_id}',
                payment_options=(
                    'card,banktransfer' if channel == CHANNEL_BANK_TRANSFER else 'card'
                ),
            )

            FlutterwaveTransaction.objects.create(
                wallet=wallet, tx_ref=tx_ref,
                channel=channel, amount=amount, currency=currency,
                phone=wallet.phone, direction='in', status='pending',
                raw_payload=result,
                timeout_at=timezone.now() + timezone.timedelta(minutes=30),
            )

            payment_url = result.get('data', {}).get('link', '')
            if payment_url:
                return redirect(payment_url)
            messages.error(request, 'Could not create payment link. Please try again.')

        except Exception:
            logger.exception('Flutterwave deposit initiation failed')
            messages.error(request, 'Card payment service unavailable. Please try a different method.')

        return redirect('flw_deposit')

    # GET
    balances = wallet.currency_balances.all().order_by('currency')
    return render(request, 'wallet/flw_deposit.html', {
        'wallet':        wallet,
        'balances':      balances,
        'home_currency': wallet.home_currency or 'KES',
        'rates_stale':   rates_are_stale(),
        'flw_public_key': getattr(settings, 'FLUTTERWAVE_CONFIG', {}).get('PUBLIC_KEY', ''),
    })


@wallet_required
def flw_redirect_view(request, wallet):
    """
    Flutterwave redirects back here after the hosted payment page.
    URL params: status, tx_ref, transaction_id
    Risk #02: always verify server-side before crediting — never trust the redirect params alone.
    """
    status         = request.GET.get('status', '')
    tx_ref         = request.GET.get('tx_ref', '')
    transaction_id = request.GET.get('transaction_id', '')

    if status != 'successful' or not tx_ref or not transaction_id:
        messages.error(request, 'Payment was not completed or was cancelled.')
        return redirect('flw_deposit')

    # Find the pending record
    try:
        flw_txn = FlutterwaveTransaction.objects.get(
            wallet=wallet, tx_ref=tx_ref, direction='in',
        )
    except FlutterwaveTransaction.DoesNotExist:
        messages.error(request, 'Unknown transaction reference.')
        return redirect('flw_deposit')

    if flw_txn.status == 'completed':
        messages.info(request, 'This payment has already been credited to your wallet.')
        return redirect('dashboard')

    # Risk #02: verify with Flutterwave before crediting
    try:
        client = FlutterwaveClient()
        verify = client.verify_transaction(transaction_id)
        data   = verify.get('data', {})

        if (
            verify.get('status') == 'success'
            and data.get('status') == 'successful'
            and data.get('tx_ref') == tx_ref
            and float(data.get('amount', 0)) >= float(flw_txn.amount)
            and data.get('currency', '').upper() == flw_txn.currency.upper()
        ):
            flw_txn.flw_tx_id   = str(transaction_id)
            flw_txn.fee         = Decimal(str(data.get('app_fee', 0)))
            flw_txn.status      = 'completed'
            flw_txn.raw_payload = data
            flw_txn.save()

            txn_type_map = {
                CHANNEL_CARD:          'flw_card_deposit',
                CHANNEL_BANK_TRANSFER: 'flw_bank_deposit',
                CHANNEL_MPESA:         'flw_mobile_deposit',
                CHANNEL_AIRTEL:        'flw_mobile_deposit',
            }
            txn_type = txn_type_map.get(flw_txn.channel, 'flw_card_deposit')
            _credit_balance(
                wallet, flw_txn.currency, flw_txn.amount,
                txn_type, external_ref=tx_ref, fee=flw_txn.fee,
            )
            messages.success(request, f'{flw_txn.currency} {flw_txn.amount:,.2f} credited to your wallet.')
        else:
            flw_txn.status = 'failed'
            flw_txn.raw_payload = data
            flw_txn.save()
            messages.error(request, 'Payment verification failed. Contact support if funds were deducted.')

    except Exception:
        logger.exception('Flutterwave payment verification failed')
        messages.error(request, 'Could not verify payment. Contact support with reference: ' + tx_ref)

    return redirect('dashboard')


@csrf_exempt
def flw_webhook(request):
    """
    Flutterwave webhook — receives payment and transfer events.
    Risk #05: verified via verif-hash header + IP allowlist.
    Risk #02: idempotency — re-checks status before any credit.
    """
    if request.method != 'POST':
        return HttpResponseBadRequest('Method not allowed')

    client = FlutterwaveClient()

    # Risk #05: IP allowlist
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    remote_ip = forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR', '')
    if not client.verify_webhook_ip(remote_ip):
        logger.warning(f'FLW webhook rejected: IP {remote_ip} not in allowlist')
        return HttpResponseForbidden('Forbidden')

    # Risk #05: secret-hash verification
    secret_hash = request.META.get('HTTP_VERIF_HASH', '')
    if not client.verify_webhook_signature(request.body, secret_hash):
        logger.warning('FLW webhook rejected: invalid verif-hash')
        return HttpResponseForbidden('Forbidden')

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest('Invalid JSON')

    event     = payload.get('event', '')
    data      = payload.get('data', {})
    tx_ref    = data.get('tx_ref', '') or data.get('reference', '')
    flw_tx_id = str(data.get('id', ''))

    logger.info(f'FLW webhook event={event} tx_ref={tx_ref} id={flw_tx_id}')

    # ── Deposit / collection confirmed ──
    if event in ('charge.completed', 'charge.failed'):
        try:
            flw_txn = FlutterwaveTransaction.objects.get(tx_ref=tx_ref, direction='in')
        except FlutterwaveTransaction.DoesNotExist:
            logger.warning(f'FLW webhook: unknown tx_ref {tx_ref}')
            return JsonResponse({'status': 'ok'})  # acknowledge anyway

        if flw_txn.status == 'completed':
            return JsonResponse({'status': 'ok'})  # already processed (Risk #02)

        if event == 'charge.completed' and data.get('status') == 'successful':
            # Server-side verify before crediting (Risk #02 defence-in-depth)
            verify = client.verify_transaction(flw_tx_id)
            vdata  = verify.get('data', {})
            if (
                verify.get('status') == 'success'
                and vdata.get('status') == 'successful'
                and vdata.get('tx_ref') == tx_ref
                and float(vdata.get('amount', 0)) >= float(flw_txn.amount)
            ):
                flw_txn.flw_tx_id   = flw_tx_id
                flw_txn.fee         = Decimal(str(vdata.get('app_fee', 0)))
                flw_txn.status      = 'completed'
                flw_txn.raw_payload = vdata
                flw_txn.save()

                txn_type_map = {
                    CHANNEL_CARD:          'flw_card_deposit',
                    CHANNEL_BANK_TRANSFER: 'flw_bank_deposit',
                    CHANNEL_MPESA:         'flw_mobile_deposit',
                    CHANNEL_AIRTEL:        'flw_mobile_deposit',
                }
                txn_type = txn_type_map.get(flw_txn.channel, 'flw_card_deposit')
                _credit_balance(
                    flw_txn.wallet, flw_txn.currency, flw_txn.amount,
                    txn_type, external_ref=tx_ref, fee=flw_txn.fee,
                )
        else:
            flw_txn.status      = 'failed'
            flw_txn.raw_payload = data
            flw_txn.save()

    # ── Transfer / payout confirmed ──
    elif event in ('transfer.completed', 'transfer.failed'):
        try:
            flw_txn = FlutterwaveTransaction.objects.get(tx_ref=tx_ref, direction='out')
        except FlutterwaveTransaction.DoesNotExist:
            logger.warning(f'FLW webhook: unknown payout tx_ref {tx_ref}')
            return JsonResponse({'status': 'ok'})

        if flw_txn.status in ('completed', 'failed'):
            return JsonResponse({'status': 'ok'})

        new_status  = 'completed' if event == 'transfer.completed' else 'failed'
        flw_txn.status      = new_status
        flw_txn.flw_tx_id   = flw_tx_id
        flw_txn.raw_payload = data
        flw_txn.save()

        if new_status == 'failed':
            # Refund the debited balance (Risk #04)
            _refund_balance(flw_txn.wallet, flw_txn.currency, flw_txn.amount, ref=tx_ref)

    return JsonResponse({'status': 'ok'})


@wallet_required
def flw_payout_view(request, wallet):
    """
    Initiate a Flutterwave payout — bank transfer or mobile money out.
    Subject to the same progressive daily/monthly/per-txn limits as other withdrawals.
    """
    if not wallet.kyc_status == 'verified' and not _sandbox.is_sandbox(wallet):
        messages.error(request, 'KYC verification required for payouts.')
        return redirect('kyc_start')

    if request.method == 'POST':
        allowed, _ = _check_rate_limit('flw_payout', str(wallet.wallet_id), 5, 3600)
        if not allowed:
            messages.error(request, 'Too many payout requests. Please wait before trying again.')
            return redirect('flw_payout')

        payout_type = request.POST.get('payout_type', 'bank')  # 'bank' | 'mobile'
        currency    = request.POST.get('currency', 'KES').strip().upper()
        amount_str  = request.POST.get('amount', '').strip()

        try:
            amount = Decimal(amount_str)
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError):
            messages.error(request, 'Invalid amount.')
            return redirect('flw_payout')

        # Progressive limit enforcement
        eff_limits    = wallet.get_effective_limits()
        daily_limit   = eff_limits['daily']
        per_txn_max   = eff_limits['per_txn']
        monthly_limit = eff_limits['monthly']

        if float(amount) > per_txn_max:
            messages.error(request, f'Single payout limit is KES {per_txn_max:,.0f} for your current tier.')
            return redirect('flw_payout')

        daily_used = wallet.get_daily_withdrawn()
        if daily_used + float(amount) > daily_limit:
            messages.error(request, f'Exceeds daily limit of KES {daily_limit:,.0f}.')
            return redirect('flw_payout')

        monthly_used = wallet.get_monthly_withdrawn()
        if monthly_used + float(amount) > monthly_limit:
            messages.error(request, f'Exceeds monthly limit of KES {monthly_limit:,.0f}.')
            return redirect('flw_payout')

        _check_aml_velocity(wallet, amount if currency == 'KES' else Decimal('0'))

        # Check balance
        try:
            cb = wallet.currency_balances.get(currency=currency)
        except CurrencyBalance.DoesNotExist:
            messages.error(request, f'You have no {currency} balance.')
            return redirect('flw_payout')

        if cb.balance < amount:
            messages.error(request, f'Insufficient {currency} balance. Available: {currency} {cb.balance:,.2f}')
            return redirect('flw_payout')

        tx_ref = f'kwallet_out_{wallet.wallet_id}_{uuid.uuid4().hex[:12]}'

        if _sandbox.is_sandbox(wallet):
            _debit_balance(wallet, currency, amount, 'flw_bank_payout', idempotency_key=tx_ref)
            FlutterwaveTransaction.objects.create(
                wallet=wallet, tx_ref=tx_ref, flw_tx_id='sandbox',
                channel='bank_payout', amount=amount, currency=currency,
                direction='out', status='completed',
                raw_payload={'sandbox': True},
            )
            messages.success(request, f'🧪 [Sandbox] Payout of {currency} {amount:,.2f} queued.')
            return redirect('dashboard')

        try:
            client  = FlutterwaveClient()
            channel = 'bank_payout'

            if payout_type == 'mobile':
                phone   = request.POST.get('phone', wallet.phone).strip()
                network = request.POST.get('network', 'mpesa')
                channel = 'mobile_payout'
                result  = client.initiate_mobile_money_payout(
                    phone=phone, amount=float(amount), currency=currency,
                    narration=f'KWallet payout {wallet.wallet_id}',
                    reference=tx_ref, network=network,
                )
            else:
                bank_code   = request.POST.get('bank_code', '').strip()
                account_num = request.POST.get('account_number', '').strip()
                account_name= request.POST.get('account_name', '').strip()
                if not bank_code or not account_num:
                    messages.error(request, 'Bank code and account number are required.')
                    return redirect('flw_payout')
                result = client.initiate_transfer(
                    account_bank=bank_code,
                    account_number=account_num,
                    amount=float(amount),
                    currency=currency,
                    narration=f'KWallet payout {wallet.wallet_id}',
                    reference=tx_ref,
                    beneficiary_name=account_name,
                )

            if result.get('status') in ('success', 'ok'):
                flw_id = str(result.get('data', {}).get('id', ''))
                # Debit balance now; refund if payout webhook comes back failed (Risk #04)
                _debit_balance(wallet, currency, amount,
                               'flw_mobile_payout' if channel == 'mobile_payout' else 'flw_bank_payout',
                               idempotency_key=tx_ref)
                FlutterwaveTransaction.objects.create(
                    wallet=wallet, tx_ref=tx_ref, flw_tx_id=flw_id,
                    channel=channel, amount=amount, currency=currency,
                    phone=request.POST.get('phone', ''),
                    direction='out', status='pending',
                    raw_payload=result.get('data', {}),
                    timeout_at=timezone.now() + timezone.timedelta(hours=24),
                )
                messages.success(request, f'Payout of {currency} {amount:,.2f} queued. Ref: {tx_ref}')
                return redirect('dashboard')
            else:
                messages.error(request, 'Payout request failed. Please try again.')

        except Exception:
            logger.exception('Flutterwave payout initiation failed')
            messages.error(request, 'Payout service unavailable. Please try again.')

        return redirect('flw_payout')

    # GET
    limits = wallet.get_effective_limits()
    tier   = wallet.get_limit_tier()
    return render(request, 'wallet/flw_payout.html', {
        'wallet':        wallet,
        'home_currency': wallet.home_currency or 'KES',
        'daily_withdrawn': float(wallet.get_daily_withdrawn()),
        'daily_limit':   limits['daily'],
        'monthly_limit': limits['monthly'],
        'per_txn_limit': limits['per_txn'],
        'daily_pct':     wallet.get_daily_pct(),
        'limit_tier':    tier,
        'limit_tier_label': LIMIT_TIERS[tier]['label'],
        'rates_stale':   rates_are_stale(),
    })


# ─────────────────────────────────────────────
# Withdraw — unified landing page
# ─────────────────────────────────────────────

@wallet_required
def withdraw_view(request, wallet):
    """Legacy /withdraw/ URL — redirects to Flutterwave payout (unified payout page)."""
    return redirect('flw_payout')



# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# Exchange
# ─────────────────────────────────────────────

@wallet_required
def exchange_view(request, wallet):
    balances = wallet.currency_balances.all().order_by('currency')
    stale    = rates_are_stale()

    if request.method == 'POST':
        if wallet.kyc_status != 'verified' and not _sandbox.is_sandbox(wallet):
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
        'home_currency': wallet.home_currency or 'KES',
        'rates_stale': stale,
    })


# ─────────────────────────────────────────────
# P2P Transfer
# ─────────────────────────────────────────────

@wallet_required
def p2p_view(request, wallet):
    balances = wallet.currency_balances.all().order_by('currency')

    if request.method == 'POST':
        if wallet.kyc_status != 'verified' and not _sandbox.is_sandbox(wallet):
            messages.error(request, 'KYC required for transfers.')
            return redirect('p2p')

        allowed, _ = _check_rate_limit('p2p', str(wallet.wallet_id), 20, 3600)
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

        # Risk #16: progressive limit enforcement for p2p sends
        if currency == 'KES':
            eff_limits    = wallet.get_effective_limits()
            daily_limit   = eff_limits['daily']
            per_txn_max   = eff_limits['per_txn']
            monthly_limit = eff_limits['monthly']

            if float(amount) > per_txn_max:
                messages.error(request, f'Single transfer limit is KES {per_txn_max:,.0f} for your current tier.')
                return redirect('p2p')

            daily_used = wallet.get_daily_withdrawn()
            if daily_used + float(total) > daily_limit:
                messages.error(request, f'Exceeds daily limit of KES {daily_limit:,.0f}.')
                return redirect('p2p')

            monthly_used = wallet.get_monthly_withdrawn()
            if monthly_used + float(total) > monthly_limit:
                messages.error(request, f'Exceeds monthly limit of KES {monthly_limit:,.0f}.')
                return redirect('p2p')

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

    limits = wallet.get_effective_limits()
    tier   = wallet.get_limit_tier()
    return render(request, 'wallet/p2p.html', {
        'wallet': wallet,
        'balances': balances,
        'home_currency': wallet.home_currency or 'KES',
        'daily_withdrawn': float(wallet.get_daily_withdrawn()),
        'daily_limit': limits['daily'],
        'monthly_limit': limits['monthly'],
        'per_txn_limit': limits['per_txn'],
        'daily_pct': wallet.get_daily_pct(),
        'limit_tier': tier,
        'limit_tier_label': LIMIT_TIERS[tier]['label'],
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
        set_home = request.POST.get('set_home') == '1'
        all_valid = [c for c, _ in EA_CURRENCIES + INTL_CURRENCIES]
        if currency not in all_valid:
            messages.error(request, 'Invalid currency.')
            return redirect('add_currency')
        if currency not in active:
            CurrencyBalance.objects.create(wallet=wallet, currency=currency, balance=0)
            active.append(currency)
            # Auto-seed sandbox starting balance for new currency
            if _sandbox.is_sandbox(wallet):
                _sandbox.seed_sandbox_balance(wallet, currency)
        # Set as home currency if requested or wallet has no home currency yet
        if set_home or not wallet.home_currency:
            wallet.home_currency = currency
            wallet.save(update_fields=['home_currency'])
        messages.success(request, f'{currency} added to your wallet.')
        if not wallet.home_currency:
            return redirect('add_currency')
        return redirect('dashboard')

    return render(request, 'wallet/add_currency.html', {
        'wallet': wallet,
        'active_currencies': active,
        'active_count': len(active),
        'max_currencies': MAX_CURRENCIES,
        'ea_currencies': EA_CURRENCIES,
        'intl_currencies': INTL_CURRENCIES,
        'no_home_currency': not wallet.home_currency,
    })


@wallet_required
def remove_currency_view(request, wallet):
    """Allow removing a currency (cannot remove home currency or currencies with balance)."""
    if request.method == 'POST':
        currency = request.POST.get('currency', '').strip().upper()
        if currency == wallet.home_currency:
            messages.error(request, f'You cannot remove your home currency ({currency}). Change your home currency first.')
            return redirect('add_currency')
        try:
            cb = wallet.currency_balances.get(currency=currency)
            if cb.balance > 0:
                messages.error(request, f'Cannot remove {currency} — balance is {cb.balance}. Exchange or transfer funds first.')
                return redirect('add_currency')
            cb.delete()
            messages.success(request, f'{currency} removed from your wallet.')
        except CurrencyBalance.DoesNotExist:
            messages.error(request, f'{currency} not found in your wallet.')
    return redirect('add_currency')


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
        rail  = request.POST.get('rail', '')
        phone = request.POST.get('phone', '').strip()
        amount_str = request.POST.get('amount', '')
        try:
            amount = req.amount if req.amount else Decimal(str(amount_str))
            amount = Decimal(str(amount))
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError, ValueError):
            messages.error(request, 'Invalid amount.')
            return render(request, 'wallet/qr_pay.html', {'req': req, 'token': token})

        if not rail:
            messages.error(request, 'Please choose a payment method.')
            return render(request, 'wallet/qr_pay.html', {'req': req, 'token': token})

        reference = 'QR' + uuid.uuid4().hex[:10].upper()
        client = FlutterwaveClient()

        try:
            if rail in ('mpesa', 'airtel'):
                # Mobile money STK push via Flutterwave
                network = 'MPESA' if rail == 'mpesa' else 'AIRTEL'
                tx_ref  = f'kwallet_qr_{reference}'
                client.initiate_mobile_money(
                    phone=phone, amount=float(amount), currency='KES',
                    tx_ref=tx_ref, network=network,
                    email='noreply@kwallet.ke',
                )
                FlutterwaveTransaction.objects.create(
                    wallet=req.wallet, tx_ref=tx_ref,
                    channel=rail, amount=amount, currency='KES',
                    phone=phone, direction='in', status='pending',
                    timeout_at=timezone.now() + timezone.timedelta(minutes=30),
                )
                if req.single_use:
                    req.status = 'paid'
                    req.save()
                return render(request, 'wallet/qr_pay_pending.html', {
                    'phone': phone, 'amount': amount,
                    'reference': reference, 'token': token,
                    'rail': 'M-Pesa' if rail == 'mpesa' else 'Airtel Money',
                })
            else:
                # Card / bank transfer — hosted Flutterwave checkout
                tx_ref = f'kwallet_qr_{reference}'
                result = client.create_payment_link(
                    amount=float(amount), currency='KES',
                    customer_email='noreply@kwallet.ke',
                    customer_name=phone or 'KWallet Customer',
                    customer_phone=phone,
                    tx_ref=tx_ref,
                    description=f'QR payment — {req.note or token[:8]}',
                    payment_options='card,banktransfer',
                )
                FlutterwaveTransaction.objects.create(
                    wallet=req.wallet, tx_ref=tx_ref,
                    channel='card', amount=amount, currency='KES',
                    phone=phone, direction='in', status='pending',
                    raw_payload=result,
                    timeout_at=timezone.now() + timezone.timedelta(minutes=30),
                )
                if req.single_use:
                    req.status = 'paid'
                    req.save()
                pay_url = result.get('data', {}).get('link', '')
                if pay_url:
                    return redirect(pay_url)
                messages.error(request, 'Could not create payment link. Please try again.')
        except Exception:
            logger.exception('QR payment initiation failed')
            messages.error(request, 'Payment initiation failed. Please try again.')

    return render(request, 'wallet/qr_pay.html', {'req': req, 'token': token})


# ─────────────────────────────────────────────
# KYC placeholder
# ─────────────────────────────────────────────

@wallet_required
def kyc_start_view(request, wallet):
    """Risk #15: KYC with document upload. In production: integrate Smile Identity / Onfido."""
    if request.method == 'POST':
        full_name  = request.POST.get('full_name', '').strip()
        id_number  = request.POST.get('id_number', '').strip()
        dob_str    = request.POST.get('dob', '').strip()
        id_front   = request.FILES.get('id_front')
        id_back    = request.FILES.get('id_back')
        selfie     = request.FILES.get('selfie')

        errors = []
        if not full_name:
            errors.append('Full legal name is required.')
        if not id_number:
            errors.append('ID / Passport number is required.')
        if not dob_str:
            errors.append('Date of birth is required.')
        if not id_front:
            errors.append('Front photo of your ID is required.')
        if not id_back:
            errors.append('Back photo of your ID is required.')
        if not selfie:
            errors.append('Selfie holding your ID is required.')

        # Validate file types
        allowed_types = ['image/jpeg', 'image/png', 'image/webp']
        for label, f in [('ID front', id_front), ('ID back', id_back), ('Selfie', selfie)]:
            if f and f.content_type not in allowed_types:
                errors.append(f'{label}: only JPEG, PNG or WebP images are accepted.')
            if f and f.size > 10 * 1024 * 1024:  # 10 MB
                errors.append(f'{label}: file must be under 10 MB.')

        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, 'wallet/kyc_start.html', {'wallet': wallet})

        try:
            from django.utils.dateparse import parse_date
            dob = parse_date(dob_str)
        except Exception:
            dob = None

        wallet.kyc_full_name = full_name
        wallet.kyc_id_number = id_number
        wallet.kyc_dob       = dob
        if id_front:
            wallet.kyc_id_front = id_front
        if id_back:
            wallet.kyc_id_back = id_back
        if selfie:
            wallet.kyc_selfie = selfie
        wallet.kyc_status = 'pending'
        wallet.save()

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


# ═══════════════════════════════════════════════════════════════════════════════
# SANDBOX / TESTING PANEL
# Only accessible when WALLET_SANDBOX_MODE=True OR wallet.is_sandbox=True.
# Completely inert on production wallets.
# ═══════════════════════════════════════════════════════════════════════════════

def _sandbox_guard(wallet):
    """Return True if this request should be allowed in sandbox mode."""
    return _sandbox.is_sandbox(wallet)


@wallet_required
def sandbox_panel_view(request, wallet):
    """
    Tester control panel — visible only to sandbox wallets.
    Shows current balances, recent sandbox transactions, and action buttons.
    """
    if not _sandbox_guard(wallet):
        messages.error(request, 'Sandbox panel is only available in sandbox mode.')
        return redirect('dashboard')

    balances = wallet.currency_balances.all().order_by('currency')
    recent = wallet.transactions.filter(
        details__icontains='sandbox'
    ).order_by('-created_at')[:20] | wallet.transactions.filter(
        external_ref__startswith='MOCK'
    ).order_by('-created_at')[:20]
    recent = wallet.transactions.filter(
        external_ref__startswith='MOCK'
    ).order_by('-created_at')[:20]

    from .rates import get_rates
    all_currencies = [c for c, _ in EA_CURRENCIES + INTL_CURRENCIES]

    return render(request, 'wallet/sandbox_panel.html', {
        'wallet': wallet,
        'balances': balances,
        'recent': recent,
        'all_currencies': all_currencies,
        'sandbox_starting': _sandbox.STARTING_BALANCE,
        'confirm_delay': _sandbox.CONFIRM_DELAY,
    })


@wallet_required
@require_POST
def sandbox_deposit_view(request, wallet):
    """Mock deposit — credits wallet immediately, no real STK sent."""
    if not _sandbox_guard(wallet):
        return JsonResponse({'ok': False, 'error': 'Not in sandbox mode'}, status=403)

    currency = request.POST.get('currency', 'KES').strip().upper()
    rail     = request.POST.get('rail', 'mpesa').strip()
    try:
        amount = Decimal(request.POST.get('amount', '0'))
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        return JsonResponse({'ok': False, 'error': 'Invalid amount'}, status=400)

    result = _sandbox.mock_stk_push(wallet, amount, currency=currency, rail=rail)
    messages.success(request, f'🧪 {result["message"]}')
    return JsonResponse({'ok': True, **result})


@wallet_required
@require_POST
def sandbox_withdraw_view(request, wallet):
    """Mock withdrawal — deducts from balance immediately."""
    if not _sandbox_guard(wallet):
        return JsonResponse({'ok': False, 'error': 'Not in sandbox mode'}, status=403)

    currency = request.POST.get('currency', 'KES').strip().upper()
    rail     = request.POST.get('rail', 'mpesa').strip()
    try:
        amount = Decimal(request.POST.get('amount', '0'))
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        return JsonResponse({'ok': False, 'error': 'Invalid amount'}, status=400)

    result = _sandbox.mock_b2c_withdraw(wallet, amount, currency=currency, rail=rail)
    if result['status'] == 'completed':
        messages.success(request, f'🧪 {result["message"]}')
    else:
        messages.error(request, f'🧪 {result["message"]}')
    return JsonResponse({'ok': result['status'] == 'completed', **result})


@wallet_required
@require_POST
def sandbox_bank_deposit_view(request, wallet):
    """Mock bank deposit — credits KES instantly."""
    if not _sandbox_guard(wallet):
        return JsonResponse({'ok': False, 'error': 'Not in sandbox mode'}, status=403)

    try:
        amount = Decimal(request.POST.get('amount', '0'))
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        return JsonResponse({'ok': False, 'error': 'Invalid amount'}, status=400)

    result = _sandbox.mock_bank_deposit(wallet, amount)
    messages.success(request, f'🧪 {result["message"]}')
    return JsonResponse({'ok': True, **result})


@wallet_required
@require_POST
def sandbox_seed_view(request, wallet):
    """Seed sandbox wallet with starting balances for all active currencies."""
    if not _sandbox_guard(wallet):
        return JsonResponse({'ok': False, 'error': 'Not in sandbox mode'}, status=403)

    seeded = []
    for cb in wallet.currency_balances.all():
        old_balance = cb.balance
        _sandbox.seed_sandbox_balance(wallet, cb.currency)
        cb.refresh_from_db()
        if cb.balance > old_balance:
            seeded.append(f'{cb.currency} +{cb.balance - old_balance}')

    if seeded:
        messages.success(request, f'🧪 Seeded: {", ".join(seeded)}')
    else:
        messages.info(request, '🧪 All currencies already have balances — no seeding needed.')
    return JsonResponse({'ok': True, 'seeded': seeded})


@wallet_required
@require_POST
def sandbox_reset_view(request, wallet):
    """Reset all sandbox balances back to zero (useful to test edge cases)."""
    if not _sandbox_guard(wallet):
        return JsonResponse({'ok': False, 'error': 'Not in sandbox mode'}, status=403)

    with db_transaction.atomic():
        wallet.currency_balances.update(balance=Decimal('0'))
        wallet.transactions.filter(external_ref__startswith='MOCK').delete()

    messages.warning(request, '🧪 All sandbox balances reset to zero and mock transactions cleared.')
    return JsonResponse({'ok': True, 'message': 'Balances zeroed and mock transactions cleared.'})


@wallet_required
@require_POST
def sandbox_exchange_view(request, wallet):
    """Mock exchange — converts between currencies instantly at live rate, fee-free."""
    if not _sandbox_guard(wallet):
        return JsonResponse({'ok': False, 'error': 'Not in sandbox mode'}, status=403)

    from_currency = request.POST.get('from_currency', '').strip().upper()
    to_currency   = request.POST.get('to_currency', '').strip().upper()
    try:
        amount = Decimal(request.POST.get('amount', '0'))
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        return JsonResponse({'ok': False, 'error': 'Invalid amount'}, status=400)

    result = _sandbox.mock_exchange(wallet, from_currency, to_currency, amount)
    if result['status'] == 'completed':
        messages.success(request, f'🧪 {result["message"]}')
    else:
        messages.error(request, f'🧪 {result["message"]}')
    return JsonResponse({'ok': result['status'] == 'completed', **result})
