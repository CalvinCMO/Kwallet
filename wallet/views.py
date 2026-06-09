"""
views.py — KWallet v2
"""
import json
import logging
from decimal import Decimal

import bcrypt
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.db import transaction as db_transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import (
    AddCurrencyForm, ExchangeForm, LoginForm,
    MpesaDepositForm, MpesaWithdrawForm,
    P2PTransferForm, RegisterForm,
)
from .models import (
    ALL_CURRENCIES, COUNTRY_HOME_CURRENCY, FEE_SCHEDULE, UNIVERSAL_CURRENCIES,
    CurrencyBalance, FeeRecord, MpesaTransaction, Transaction, Wallet,
    WalletLimit, calculate_fee, create_default_wallet_limit,
)
from .mpesa import MpesaClient
from . import rates as rate_service
from . import settlement as settlement_service

logger = logging.getLogger(__name__)

CURRENCY_SYMBOLS = {
    'KES':'KSh','TZS':'TSh','UGX':'USh','RWF':'RF','ETB':'Br',
    'USD':'$','EUR':'€','GBP':'£','JPY':'¥','CNY':'¥',
    'AED':'د.إ','INR':'₹','CAD':'C$','AUD':'A$','CHF':'Fr',
    'ZAR':'R','NGN':'₦','GHS':'₵','XOF':'CFA','MUR':'₨',
}
CURRENCY_FLAGS = {
    'KES':'🇰🇪','TZS':'🇹🇿','UGX':'🇺🇬','RWF':'🇷🇼','ETB':'🇪🇹',
    'USD':'🇺🇸','EUR':'🇪🇺','GBP':'🇬🇧','JPY':'🇯🇵','CNY':'🇨🇳',
    'AED':'🇦🇪','INR':'🇮🇳','CAD':'🇨🇦','AUD':'🇦🇺','CHF':'🇨🇭',
    'ZAR':'🇿🇦','NGN':'🇳🇬','GHS':'🇬🇭','XOF':'🌍','MUR':'🇲🇺',
}


# ── helpers ───────────────────────────────────────────────────────────────────

def get_or_create_balance(wallet, currency):
    obj, _ = CurrencyBalance.objects.get_or_create(
        wallet=wallet, currency=currency,
        defaults={'balance': Decimal('0.0000')}
    )
    return obj


def check_pin(wallet, pin):
    try:
        return bcrypt.checkpw(pin.encode('utf-8'), wallet.pin_hash.encode('utf-8'))
    except Exception:
        return False


def wallet_required(view_func):
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        try:
            request.wallet = request.user.wallet
        except Wallet.DoesNotExist:
            messages.error(request, 'No wallet found.')
            return redirect('login')
        return view_func(request, *args, **kwargs)
    wrapper.__name__ = view_func.__name__
    return wrapper


def _collect_fee(wallet, transaction_type, amount, currency, linked_txn):
    """Deducts fee from wallet balance and records FeeRecord."""
    fee = calculate_fee(transaction_type, amount)
    if fee <= 0:
        return Decimal('0')
    bal = get_or_create_balance(wallet, currency)
    if bal.balance >= fee:
        bal.balance -= fee
        bal.save()
        FeeRecord.objects.create(
            transaction=linked_txn,
            wallet=wallet,
            amount=fee,
            currency=currency,
            fee_type=transaction_type,
        )
    return fee


def _credit_balance(wallet, currency, amount, receipt, ref, txn_type='mpesa_deposit'):
    """
    Credits a currency balance, creates a completed Transaction, and records
    the real-money movement in the PoolLedger so CompanyAccount.ledger_balance
    stays in sync with every deposit as it arrives.
    """
    bal = get_or_create_balance(wallet, currency)
    bal.balance += amount
    bal.save()
    txn = Transaction.objects.create(
        wallet=wallet,
        transaction_type=txn_type,
        currency=currency,
        amount=amount,
        status='completed',
        details=f'Deposit confirmed. Receipt: {receipt}',
        reference=ref,
    )
    # Record real-money inflow into the client float pool.
    # Non-fatal: if no CompanyAccount is configured yet, log a warning
    # so the deposit still goes through during early development.
    try:
        settlement_service.record_deposit(txn, created_by='mpesa_callback')
    except Exception as e:
        logger.warning(f"_credit_balance: pool ledger write skipped — {e}")
    return txn


# ── auth ──────────────────────────────────────────────────────────────────────

def register_view(request):
    form = RegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        data    = form.cleaned_data
        parts   = data['full_name'].split(' ', 1)
        first   = parts[0]
        last    = parts[1] if len(parts) > 1 else ''
        country = data['country']

        pin_hash = bcrypt.hashpw(
            data['pin'].encode('utf-8'), bcrypt.gensalt()
        ).decode('utf-8')

        with db_transaction.atomic():
            user = User.objects.create_user(
                username=data['phone'], first_name=first, last_name=last
            )
            user.set_unusable_password()
            user.save()

            wallet = Wallet.objects.create(
                user=user, phone=data['phone'],
                pin_hash=pin_hash, country=country,
            )

            # All 5 EA currencies for every wallet
            for curr in UNIVERSAL_CURRENCIES:
                CurrencyBalance.objects.create(wallet=wallet, currency=curr,
                                               balance=Decimal('0.0000'))

            # 5 user-chosen international currencies
            chosen = form.get_chosen_currencies()
            for curr in chosen:
                if curr not in UNIVERSAL_CURRENCIES:
                    CurrencyBalance.objects.create(wallet=wallet, currency=curr,
                                                   balance=Decimal('0.0000'))

            # Create default USD-based transaction limits for the new wallet
            create_default_wallet_limit(wallet)

        logger.info(f"Registered: {wallet.wallet_id} | {wallet.phone} | {country}")
        messages.success(request, f'Wallet created! You have {5 + len(set(chosen) - set(UNIVERSAL_CURRENCIES))} currency balances ready.')
        return redirect('login')

    return render(request, 'wallet/register.html', {'form': form})


def login_view(request):
    form = LoginForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        phone = form.cleaned_data['phone'].strip()
        pin   = form.cleaned_data['pin']
        try:
            wallet = Wallet.objects.select_related('user').get(phone=phone)
        except Wallet.DoesNotExist:
            messages.error(request, 'No wallet found for that phone number.')
            return render(request, 'wallet/login.html', {'form': form})
        if check_pin(wallet, pin):
            login(request, wallet.user,
                  backend='django.contrib.auth.backends.ModelBackend')
            return redirect('dashboard')
        messages.error(request, 'Incorrect PIN.')
    return render(request, 'wallet/login.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('login')


# ── dashboard ─────────────────────────────────────────────────────────────────

@wallet_required
def dashboard(request):
    wallet      = request.wallet
    balances    = {cb.currency: cb for cb in wallet.currency_balances.all()}
    recent_txns = wallet.transactions.all()[:10]
    home_curr   = wallet.home_currency
    total       = rate_service.get_portfolio_value(balances, home_curr)

    return render(request, 'wallet/dashboard.html', {
        'wallet':           wallet,
        'balances':         balances,
        'recent_txns':      recent_txns,
        'currency_symbols': CURRENCY_SYMBOLS,
        'currency_flags':   CURRENCY_FLAGS,
        'total_value':      total,
        'home_currency':    home_curr,
        'fee_schedule': {
            k: (
                "Tiered: 1.5% → 1.0% → 0.75% → 0.5%"
                if v.get('type') == 'exchange_tiered'
                else "Tiered (M-Pesa rates)"
                if v.get('type') == 'tiered'
                else f"{v.get('pct', Decimal('0')) * 100:.1f}%"
            )
            for k, v in FEE_SCHEDULE.items()
        },
    })


def rates_api(request):
    """GET /api/rates/ — returns all pairs as JSON for JS use."""
    flat = rate_service.get_rates()
    return JsonResponse({k: str(v) for k, v in flat.items()})


# ── add / manage currencies ───────────────────────────────────────────────────

@wallet_required
def add_currency_view(request):
    wallet = request.wallet
    form   = AddCurrencyForm(request.POST or None, wallet=wallet)
    if request.method == 'POST' and form.is_valid():
        currency = form.cleaned_data['currency']
        CurrencyBalance.objects.create(
            wallet=wallet, currency=currency, balance=Decimal('0.0000')
        )
        messages.success(request, f'{currency} balance added to your wallet.')
        return redirect('dashboard')
    existing = wallet.get_active_currencies()
    return render(request, 'wallet/add_currency.html', {
        'form': form, 'existing': existing
    })


# ── exchange ──────────────────────────────────────────────────────────────────

@wallet_required
def exchange_view(request):
    wallet       = request.wallet
    flat_rates   = rate_service.get_rates()
    nested_rates = rate_service.get_rates_for_display()
    active_currs = wallet.get_active_currencies()

    # Build choices limited to what the user has activated
    from .models import ALL_CURRENCIES as AC
    user_choices = [(c, n) for c, n in AC if c in active_currs]

    from .forms import ExchangeForm as EF
    form = EF(request.POST or None)
    # Restrict choices to user's active currencies
    form.fields['from_currency'].choices = user_choices
    form.fields['to_currency'].choices   = user_choices

    if request.method == 'POST' and form.is_valid():
        from_curr = form.cleaned_data['from_currency']
        to_curr   = form.cleaned_data['to_currency']
        amount    = Decimal(str(form.cleaned_data['amount']))
        fee       = calculate_fee('exchange', amount, currency=from_curr)
        total_needed = amount + fee

        # ── Limit checks ─────────────────────────────────────────────────────
        try:
            wallet_limit = wallet.limit
            ok, err = wallet_limit.check_all(amount, from_curr)
            if not ok:
                messages.error(request, err)
                return render(request, 'wallet/exchange.html', {
                    'form': form, 'flat_rates': flat_rates,
                    'nested_rates': nested_rates, 'active_currs': active_currs,
                })
        except WalletLimit.DoesNotExist:
            pass  # No limit record means uncapped (admin wallets, etc.)

        from_bal = get_or_create_balance(wallet, from_curr)
        if from_bal.balance < total_needed:
            messages.error(
                request,
                f'Insufficient {from_curr}. Need {total_needed:.4f} '
                f'({amount:.4f} + {fee:.4f} fee).'
            )
        else:
            try:
                converted = rate_service.convert(amount, from_curr, to_curr)
            except ValueError as e:
                messages.error(request, str(e))
                return render(request, 'wallet/exchange.html', {
                    'form': form, 'flat_rates': flat_rates,
                    'nested_rates': nested_rates,
                })

            to_bal = get_or_create_balance(wallet, to_curr)
            with db_transaction.atomic():
                from_bal.balance -= total_needed
                to_bal.balance   += converted
                from_bal.save()
                to_bal.save()

                txn = Transaction.objects.create(
                    wallet=wallet, transaction_type='exchange',
                    currency=from_curr, amount=amount, fee=fee,
                    status='completed',
                    details=f'Exchanged {amount} {from_curr} → {converted} {to_curr} | Fee: {fee} {from_curr}',
                )
                if fee > 0:
                    FeeRecord.objects.create(
                        transaction=txn, wallet=wallet,
                        amount=fee, currency=from_curr, fee_type='exchange'
                    )

            messages.success(
                request,
                f'Exchanged {amount} {from_curr} → {converted} {to_curr}. '
                f'Fee: {fee} {from_curr}.'
            )
            return redirect('dashboard')

    return render(request, 'wallet/exchange.html', {
        'form': form, 'flat_rates': flat_rates, 'nested_rates': nested_rates,
        'active_currs': active_currs,
    })


# ── p2p transfer ──────────────────────────────────────────────────────────────

@wallet_required
def p2p_view(request):
    wallet       = request.wallet
    active_currs = wallet.get_active_currencies()
    from .models import ALL_CURRENCIES as AC
    user_choices = [(c, n) for c, n in AC if c in active_currs]

    form = P2PTransferForm(request.POST or None)
    form.fields['currency'].choices = user_choices

    if request.method == 'POST' and form.is_valid():
        recipient_phone = form.cleaned_data['recipient_phone']
        currency        = form.cleaned_data['currency']
        amount          = Decimal(str(form.cleaned_data['amount']))
        fee             = calculate_fee('p2p_send', amount)
        total_needed    = amount + fee

        if recipient_phone == wallet.phone:
            messages.error(request, 'Cannot transfer to yourself.')
        else:
            # ── Limit checks ────────────────────────────────────────────────
            try:
                wallet_limit = wallet.limit
                ok, err = wallet_limit.check_all(amount, currency)
                if not ok:
                    messages.error(request, err)
                    return render(request, 'wallet/p2p.html', {'form': form})
            except WalletLimit.DoesNotExist:
                pass
            from_bal = get_or_create_balance(wallet, currency)
            if from_bal.balance < total_needed:
                messages.error(
                    request,
                    f'Insufficient {currency}. Need {total_needed:.4f} '
                    f'({amount:.4f} + {fee:.4f} fee).'
                )
            else:
                recipient_wallet = get_object_or_404(Wallet, phone=recipient_phone)
                to_bal = get_or_create_balance(recipient_wallet, currency)

                with db_transaction.atomic():
                    from_bal.balance -= total_needed
                    to_bal.balance   += amount
                    from_bal.save()
                    to_bal.save()

                    send_txn = Transaction.objects.create(
                        wallet=wallet, transaction_type='p2p_send',
                        currency=currency, amount=amount, fee=fee,
                        status='completed',
                        details=f'Sent to {recipient_wallet.user.get_full_name()} ({recipient_phone}) | Fee: {fee} {currency}',
                    )
                    Transaction.objects.create(
                        wallet=recipient_wallet, transaction_type='p2p_receive',
                        currency=currency, amount=amount, fee=Decimal('0'),
                        status='completed',
                        details=f'Received from {wallet.user.get_full_name()} ({wallet.phone})',
                    )
                    if fee > 0:
                        FeeRecord.objects.create(
                            transaction=send_txn, wallet=wallet,
                            amount=fee, currency=currency, fee_type='p2p_send'
                        )

                messages.success(
                    request,
                    f'Sent {amount} {currency} to {recipient_wallet.user.get_full_name()}. '
                    f'Fee: {fee} {currency}.'
                )
                return redirect('dashboard')

    return render(request, 'wallet/p2p.html', {'form': form})


# ── M-Pesa deposit ────────────────────────────────────────────────────────────

@wallet_required
def mpesa_deposit_view(request):
    wallet = request.wallet
    form   = MpesaDepositForm(request.POST or None)
    fee_info = {'pct': '0%', 'note': 'No deposit fee — we absorb it for you.'}

    if request.method == 'POST' and form.is_valid():
        amount = float(form.cleaned_data['amount'])
        phone  = form.cleaned_data.get('phone') or wallet.phone

        result = MpesaClient().stk_push(
            phone=phone, amount=amount,
            account_ref=wallet.wallet_id[:12],
            description='KWallet Dep',
        )
        if result['success']:
            MpesaTransaction.objects.create(
                wallet=wallet, phone=phone, amount=amount,
                checkout_request_id=result['checkout_request_id'],
                merchant_request_id=result['merchant_request_id'],
                direction='in', status='pending',
            )
            messages.success(request, result['message'])
            return redirect('mpesa_pending',
                            checkout_id=result['checkout_request_id'])
        else:
            messages.error(request, result['message'])

    return render(request, 'wallet/mpesa_deposit.html', {
        'form': form, 'wallet': wallet, 'fee_info': fee_info
    })


@wallet_required
def mpesa_pending_view(request, checkout_id):
    try:
        mpesa_txn = MpesaTransaction.objects.get(
            checkout_request_id=checkout_id, wallet=request.wallet,
        )
    except MpesaTransaction.DoesNotExist:
        messages.error(request, 'Transaction not found.')
        return redirect('dashboard')
    return render(request, 'wallet/mpesa_pending.html', {'mpesa_txn': mpesa_txn})


@wallet_required
def mpesa_status_api(request, checkout_id):
    try:
        txn = MpesaTransaction.objects.get(
            checkout_request_id=checkout_id, wallet=request.wallet,
        )
        return JsonResponse({'status': txn.status, 'receipt': txn.mpesa_receipt})
    except MpesaTransaction.DoesNotExist:
        return JsonResponse({'status': 'not_found'})


# ── M-Pesa withdrawal ─────────────────────────────────────────────────────────

@wallet_required
def mpesa_withdraw_view(request):
    wallet = request.wallet
    form   = MpesaWithdrawForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        amount      = Decimal(str(form.cleaned_data['amount']))
        phone       = form.cleaned_data.get('phone') or wallet.phone
        fee         = calculate_fee('mpesa_withdraw', amount)
        total_needed = amount + fee
        kes_bal     = get_or_create_balance(wallet, 'KES')

        # ── Limit checks (KES amount vs USD-equivalent limits) ──────────────
        try:
            wallet_limit = wallet.limit
            ok, err = wallet_limit.check_all(amount, 'KES')
            if not ok:
                messages.error(request, err)
                return render(request, 'wallet/mpesa_withdraw.html',
                              {'form': form, 'wallet': wallet, 'withdraw_fee_pct': '1%'})
        except WalletLimit.DoesNotExist:
            pass


        if kes_bal.balance < total_needed:
            messages.error(
                request,
                f'Insufficient KES. Need KES {total_needed:.2f} '
                f'({amount:.2f} + {fee:.2f} fee).'
            )
        else:
            result = MpesaClient().b2c_payment(
                phone=phone, amount=float(amount)
            )
            if result['success']:
                with db_transaction.atomic():
                    kes_bal.balance -= total_needed
                    kes_bal.save()
                    txn = Transaction.objects.create(
                        wallet=wallet, transaction_type='mpesa_withdraw',
                        currency='KES', amount=amount, fee=fee,
                        status='pending',
                        details=f'M-Pesa withdrawal to {phone} | Fee: KES {fee}',
                        reference=result.get('conversation_id', ''),
                    )
                    if fee > 0:
                        FeeRecord.objects.create(
                            transaction=txn, wallet=wallet,
                            amount=fee, currency='KES', fee_type='mpesa_withdraw'
                        )
                messages.success(request, result['message'])
                return redirect('dashboard')
            else:
                messages.error(request, result['message'])

    return render(request, 'wallet/mpesa_withdraw.html', {
        'form': form, 'wallet': wallet,
        'withdraw_fee_pct': '1%',
    })


# ── M-Pesa callback ───────────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def mpesa_callback(request):
    try:
        body        = json.loads(request.body)
        parsed      = MpesaClient.parse_stk_callback(body)
        checkout_id = parsed.get('checkout_request_id')
        mpesa_txn   = MpesaTransaction.objects.filter(
            checkout_request_id=checkout_id
        ).first()

        if not mpesa_txn:
            return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})

        if parsed['success']:
            with db_transaction.atomic():
                mpesa_txn.status        = 'completed'
                mpesa_txn.mpesa_receipt = parsed.get('receipt', '')
                mpesa_txn.result_code   = parsed.get('result_code', '')
                mpesa_txn.result_desc   = parsed.get('result_desc', '')
                mpesa_txn.save()
                _credit_balance(
                    wallet=mpesa_txn.wallet,
                    currency='KES',
                    amount=Decimal(str(parsed['amount'])),
                    receipt=parsed.get('receipt', ''),
                    ref=checkout_id,
                )
        else:
            mpesa_txn.status      = 'failed'
            mpesa_txn.result_code = parsed.get('result_code', '')
            mpesa_txn.result_desc = parsed.get('result_desc', 'Failed')
            mpesa_txn.save()

        return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})
    except Exception as e:
        logger.error(f"Callback error: {e}", exc_info=True)
        return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@csrf_exempt
@require_POST
def b2c_result(request):
    try:
        body    = json.loads(request.body)
        parsed  = MpesaClient.parse_b2c_result(body)
        conv_id = parsed.get('conversation_id')
        txn     = Transaction.objects.filter(reference=conv_id).first()

        if txn:
            with db_transaction.atomic():
                if parsed['success']:
                    txn.status  = 'completed'
                    txn.details = (
                        f"M-Pesa withdrawal confirmed. "
                        f"Receipt: {parsed.get('receipt','')} | "
                        f"To: {parsed.get('receiver','')}"
                    )
                    txn.save()
                    # Record real-money outflow from client float pool.
                    try:
                        settlement_service.record_withdrawal(txn, created_by='b2c_callback')
                    except Exception as e:
                        logger.warning(f"b2c_result: pool ledger write skipped — {e}")
                else:
                    txn.status  = 'failed'
                    txn.details = f"B2C failed: {parsed.get('result_desc','')}"
                    # Refund amount + fee back to user wallet balance
                    kes_bal = get_or_create_balance(txn.wallet, 'KES')
                    kes_bal.balance += txn.amount + txn.fee
                    kes_bal.save()
                    txn.save()

        return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})
    except Exception as e:
        logger.error(f"B2C result error: {e}", exc_info=True)
        return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@csrf_exempt
@require_POST
def b2c_timeout(request):
    try:
        body    = json.loads(request.body)
        result  = body.get('Result', {})
        conv_id = result.get('ConversationID')
        if conv_id:
            with db_transaction.atomic():
                txn = Transaction.objects.filter(
                    reference=conv_id, status='pending'
                ).first()
                if txn:
                    txn.status  = 'failed'
                    txn.details = 'B2C timed out — balance refunded'
                    txn.save()
                    kes_bal = get_or_create_balance(txn.wallet, 'KES')
                    kes_bal.balance += txn.amount + txn.fee
                    kes_bal.save()
        return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})
    except Exception as e:
        logger.error(f"B2C timeout error: {e}", exc_info=True)
        return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@wallet_required
def stk_query(request, checkout_id):
    try:
        txn = MpesaTransaction.objects.get(
            checkout_request_id=checkout_id, wallet=request.wallet,
        )
    except MpesaTransaction.DoesNotExist:
        return JsonResponse({'status': 'not_found'})

    if txn.status in ('completed', 'failed'):
        return JsonResponse({'status': txn.status, 'receipt': txn.mpesa_receipt})

    result = MpesaClient().query_stk(checkout_id)
    if result.get('success'):
        with db_transaction.atomic():
            txn.status = 'completed'
            txn.save()
            _credit_balance(
                wallet=txn.wallet, currency='KES', amount=txn.amount,
                receipt=txn.mpesa_receipt or checkout_id[-8:],
                ref=checkout_id,
            )
    elif result.get('result_code') in ('1032', '1037', '2001'):
        txn.status = 'failed'
        txn.save()

    return JsonResponse({'status': txn.status, 'receipt': txn.mpesa_receipt})


# ── mock complete (dev only) ──────────────────────────────────────────────────

def mpesa_mock_complete(request, checkout_id):
    from django.conf import settings
    if not settings.DEBUG:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Not available in production.")

    try:
        mpesa_txn = MpesaTransaction.objects.get(checkout_request_id=checkout_id)
    except MpesaTransaction.DoesNotExist:
        messages.error(request, 'Transaction not found.')
        return redirect('dashboard')

    if mpesa_txn.status == 'completed':
        messages.info(request, 'Already completed.')
        return redirect('dashboard')

    receipt = f'MOCK{checkout_id[-8:].upper()}'
    with db_transaction.atomic():
        mpesa_txn.status        = 'completed'
        mpesa_txn.mpesa_receipt = receipt
        mpesa_txn.result_code   = '0'
        mpesa_txn.result_desc   = 'Mock Success'
        mpesa_txn.save()
        _credit_balance(
            wallet=mpesa_txn.wallet, currency='KES',
            amount=mpesa_txn.amount, receipt=receipt, ref=checkout_id,
        )

    messages.success(request, f'[MOCK] KES {mpesa_txn.amount} credited. Receipt: {receipt}')
    return redirect('dashboard')


# ── transaction history ───────────────────────────────────────────────────────

@wallet_required
def transactions_view(request):
    wallet          = request.wallet
    txns            = wallet.transactions.all()
    currency_filter = request.GET.get('currency', '')
    type_filter     = request.GET.get('type', '')
    if currency_filter:
        txns = txns.filter(currency=currency_filter)
    if type_filter:
        txns = txns.filter(transaction_type=type_filter)

    return render(request, 'wallet/transactions.html', {
        'txns':             txns,
        'currencies':       wallet.get_active_currencies(),
        'currency_filter':  currency_filter,
        'type_filter':      type_filter,
        'currency_symbols': CURRENCY_SYMBOLS,
    })


# ── health check ──────────────────────────────────────────────────────────────

def health_check(request):
    from django.db import connection
    from django.conf import settings as dj_settings
    db_ok = False
    try:
        connection.ensure_connection()
        db_ok = True
    except Exception:
        pass
    return JsonResponse({
        'status':      'ok' if db_ok else 'degraded',
        'database':    'ok' if db_ok else 'error',
        'environment': dj_settings.MPESA_CONFIG.get('ENVIRONMENT', 'unknown'),
        'mock_mode':   dj_settings.MPESA_CONFIG.get('USE_MOCK', False),
        'version':     '2.0.0',
    })
