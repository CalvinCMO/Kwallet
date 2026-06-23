"""
sandbox.py — KWallet Sandbox / Mock Rail

Provides complete in-process simulation of every payment rail so that
testers can exercise the full deposit → exchange → withdraw → P2P flow
on the live Railway deployment without touching real money.

Safe to leave enabled in production for individual sandbox-flagged wallets
(useful for support demos, stress testing, and QA).

Controlled by:
  settings.WALLET_SANDBOX_MODE   — global on/off (env: WALLET_SANDBOX_MODE)
  wallet.is_sandbox              — per-wallet flag

A wallet is in sandbox if EITHER the global flag is True OR its own flag is True.
This means you can go live globally and still keep specific test accounts sandboxed.
"""

import uuid
import logging
import time
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django.db import transaction as db_transaction

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
SANDBOX_MODE       = getattr(settings, 'WALLET_SANDBOX_MODE', True)
CONFIRM_DELAY      = getattr(settings, 'SANDBOX_CONFIRM_DELAY', 3)   # seconds
STARTING_BALANCE   = getattr(settings, 'SANDBOX_STARTING_BALANCE', {
    'KES': 10000, 'USD': 100, 'EUR': 100, 'GBP': 100,
    'TZS': 250000, 'UGX': 400000,
})

# Sandbox receipts always start with this prefix so they're easy to spot in logs
MOCK_RECEIPT_PREFIX = 'MOCK'


def is_sandbox(wallet) -> bool:
    """Return True if this wallet should use mock rails."""
    return SANDBOX_MODE or getattr(wallet, 'is_sandbox', False)


def mock_ref(prefix: str = 'MOCK') -> str:
    """Generate a unique mock transaction reference."""
    return f'{prefix}{uuid.uuid4().hex[:10].upper()}'


# ── Mock STK Push (Deposit) ───────────────────────────────────────────────────

def mock_stk_push(wallet, amount: Decimal, currency: str = 'KES',
                  phone: str = '', rail: str = 'mpesa') -> dict:
    """
    Simulate an STK push deposit.
    Immediately credits the wallet balance (no async callback needed).
    Returns a result dict mimicking what the real client would give back,
    plus extra sandbox metadata.
    """
    from .models import MpesaTransaction, CurrencyBalance
    from .views import _credit_balance

    receipt = mock_ref('MOCK_DEP_')
    checkout_id = f'mock_stk_{int(time.time())}_{uuid.uuid4().hex[:6]}'

    with db_transaction.atomic():
        # Create a completed transaction record (skips pending → callback flow)
        MpesaTransaction.objects.create(
            wallet=wallet,
            checkout_request_id=checkout_id,
            merchant_request_id='mock',
            amount=amount,
            phone=phone or wallet.phone,
            status='completed',
            transaction_type=f'{rail}_deposit',
            timeout_at=timezone.now() + timezone.timedelta(minutes=1),
            mpesa_receipt=receipt,
        )
        # Ensure currency balance exists
        CurrencyBalance.objects.get_or_create(wallet=wallet, currency=currency,
                                               defaults={'balance': 0})
        _credit_balance(wallet, currency, amount,
                        f'{rail}_deposit', external_ref=receipt, fee=Decimal('0'))

    logger.info('[SANDBOX] Mock %s deposit: wallet=%s amount=%s %s receipt=%s',
                rail, wallet.wallet_id, amount, currency, receipt)
    return {
        'sandbox': True,
        'receipt': receipt,
        'checkout_request_id': checkout_id,
        'amount': str(amount),
        'currency': currency,
        'rail': rail,
        'status': 'completed',
        'message': f'Sandbox {rail.upper()} deposit of {currency} {amount} credited instantly.',
    }


# ── Mock B2C Withdrawal ───────────────────────────────────────────────────────

def mock_b2c_withdraw(wallet, amount: Decimal, currency: str = 'KES',
                      phone: str = '', rail: str = 'mpesa') -> dict:
    """
    Simulate a B2C / disbursement withdrawal.
    Deducts from balance immediately (no async result callback).
    """
    from .models import MpesaTransaction, CurrencyBalance
    from .views import _debit_balance

    receipt = mock_ref('MOCK_WD_')
    try:
        with db_transaction.atomic():
            cb = CurrencyBalance.objects.select_for_update().get(
                wallet=wallet, currency=currency
            )
            if cb.balance < amount:
                return {
                    'sandbox': True, 'status': 'failed',
                    'message': f'Insufficient {currency} balance ({cb.balance} < {amount}).',
                }
            _debit_balance(wallet, currency, amount,
                           f'{rail}_withdraw', external_ref=receipt, fee=Decimal('0'))
            MpesaTransaction.objects.create(
                wallet=wallet,
                checkout_request_id=f'mock_b2c_{int(time.time())}',
                merchant_request_id='mock',
                amount=amount,
                phone=phone or wallet.phone,
                status='completed',
                transaction_type=f'{rail}_withdraw',
                timeout_at=timezone.now() + timezone.timedelta(minutes=1),
                mpesa_receipt=receipt,
            )
    except CurrencyBalance.DoesNotExist:
        return {'sandbox': True, 'status': 'failed',
                'message': f'No {currency} balance found on this wallet.'}

    logger.info('[SANDBOX] Mock %s withdrawal: wallet=%s amount=%s %s receipt=%s',
                rail, wallet.wallet_id, amount, currency, receipt)
    return {
        'sandbox': True, 'receipt': receipt, 'amount': str(amount),
        'currency': currency, 'rail': rail, 'status': 'completed',
        'message': f'Sandbox withdrawal of {currency} {amount} deducted instantly.',
    }


# ── Mock Bank Deposit ─────────────────────────────────────────────────────────

def mock_bank_deposit(wallet, amount: Decimal) -> dict:
    """Simulate a confirmed bank/PesaLink deposit."""
    from .models import BankTransaction, CurrencyBalance
    from .views import _credit_balance

    receipt = mock_ref('MOCK_BNK_')
    with db_transaction.atomic():
        CurrencyBalance.objects.get_or_create(wallet=wallet, currency='KES',
                                               defaults={'balance': 0})
        BankTransaction.objects.create(
            wallet=wallet, pesalink_ref=receipt,
            amount=amount, bank_name='Sandbox Bank',
            account_number='0000000000', account_name='Sandbox Tester',
            status='completed', transaction_type='bank_deposit',
            timeout_at=timezone.now(),
        )
        _credit_balance(wallet, 'KES', amount, 'bank_deposit',
                        external_ref=receipt, fee=Decimal('0'))

    logger.info('[SANDBOX] Mock bank deposit: wallet=%s amount=%s receipt=%s',
                wallet.wallet_id, amount, receipt)
    return {
        'sandbox': True, 'receipt': receipt, 'amount': str(amount),
        'status': 'completed',
        'message': f'Sandbox bank deposit of KES {amount} credited instantly.',
    }


# ── Seed Starting Balance ─────────────────────────────────────────────────────

def seed_sandbox_balance(wallet, currency: str):
    """
    Credit a fresh sandbox wallet with the configured starting balance
    for `currency`. Called automatically when a sandbox user adds a currency.
    Only runs if the balance is currently 0 and the wallet is in sandbox.
    """
    if not is_sandbox(wallet):
        return
    seed_amount = STARTING_BALANCE.get(currency)
    if not seed_amount:
        return

    from .models import CurrencyBalance
    from .views import _credit_balance

    cb, _ = CurrencyBalance.objects.get_or_create(
        wallet=wallet, currency=currency, defaults={'balance': 0}
    )
    if cb.balance > 0:
        return  # already has funds — don't double-seed

    receipt = mock_ref('MOCK_SEED_')
    _credit_balance(wallet, currency, Decimal(str(seed_amount)),
                    'sandbox_seed', external_ref=receipt, fee=Decimal('0'))
    logger.info('[SANDBOX] Seeded %s %s for wallet %s',
                currency, seed_amount, wallet.wallet_id)


# ── Mock Exchange ─────────────────────────────────────────────────────────────

def mock_exchange(wallet, from_currency: str, to_currency: str, amount: Decimal) -> dict:
    """
    Simulate a currency exchange using live rates (fee-free for sandbox).
    Debits from_currency and credits to_currency immediately.
    """
    from .models import CurrencyBalance
    from .views import _debit_balance, _credit_balance
    from .rates import get_pair_rate

    if from_currency == to_currency:
        return {'sandbox': True, 'status': 'failed',
                'message': 'From and To currencies must be different.'}

    try:
        rate = Decimal(str(get_pair_rate(from_currency, to_currency)))
    except Exception:
        return {'sandbox': True, 'status': 'failed',
                'message': f'Exchange rate unavailable for {from_currency}/{to_currency}.'}

    converted = (amount * rate).quantize(Decimal('0.000001'))
    receipt = mock_ref('MOCK_EX_')

    try:
        with db_transaction.atomic():
            from_cb = CurrencyBalance.objects.select_for_update().get(
                wallet=wallet, currency=from_currency
            )
            if from_cb.balance < amount:
                return {
                    'sandbox': True, 'status': 'failed',
                    'message': f'Insufficient {from_currency} balance ({from_cb.balance} < {amount}).',
                }
            to_cb, _ = CurrencyBalance.objects.get_or_create(
                wallet=wallet, currency=to_currency, defaults={'balance': 0}
            )
            _debit_balance(wallet, from_currency, amount,
                           'sandbox_exchange', external_ref=receipt, fee=Decimal('0'))
            _credit_balance(wallet, to_currency, converted,
                            'sandbox_exchange', external_ref=receipt, fee=Decimal('0'))
    except CurrencyBalance.DoesNotExist:
        return {'sandbox': True, 'status': 'failed',
                'message': f'No {from_currency} balance found on this wallet.'}

    logger.info('[SANDBOX] Mock exchange: wallet=%s %s %s → %s %s @ %s receipt=%s',
                wallet.wallet_id, amount, from_currency, converted, to_currency, rate, receipt)
    return {
        'sandbox': True,
        'receipt': receipt,
        'from_currency': from_currency,
        'to_currency': to_currency,
        'amount': str(amount),
        'converted': str(converted),
        'rate': str(rate),
        'status': 'completed',
        'message': (f'Sandbox exchange: {from_currency} {amount} → '
                    f'{to_currency} {converted} @ {rate:.6f} (fee-free).'),
    }


# ── Mock Bank Withdraw ────────────────────────────────────────────────────────

def mock_bank_withdraw(wallet, amount: Decimal, bank_name: str = '',
                       account_number: str = '', account_name: str = '') -> dict:
    """
    Simulate a bank/PesaLink withdrawal.
    Deducts KES balance immediately, no real bank rails called.
    """
    from .models import BankTransaction, CurrencyBalance
    from .views import _debit_balance

    receipt = mock_ref('MOCK_BANK_WD_')
    try:
        with db_transaction.atomic():
            cb = CurrencyBalance.objects.select_for_update().get(
                wallet=wallet, currency='KES'
            )
            if cb.balance < amount:
                return {
                    'sandbox': True, 'status': 'failed',
                    'message': f'Insufficient KES balance ({cb.balance} < {amount}).',
                }
            _debit_balance(wallet, 'KES', amount,
                           'bank_withdraw', external_ref=receipt, fee=Decimal('0'))
            BankTransaction.objects.create(
                wallet=wallet,
                reference=receipt,
                amount=amount,
                bank_name=bank_name or 'Mock Bank',
                account_number=account_number or '0000000000',
                account_name=account_name or 'Sandbox User',
                status='completed',
                transaction_type='bank_withdraw',
            )
    except CurrencyBalance.DoesNotExist:
        return {'sandbox': True, 'status': 'failed',
                'message': 'No KES balance found on this wallet.'}

    logger.info('[SANDBOX] Mock bank withdrawal: wallet=%s amount=%s receipt=%s',
                wallet.wallet_id, amount, receipt)
    return {
        'sandbox': True, 'receipt': receipt,
        'amount': str(amount), 'currency': 'KES',
        'bank': bank_name, 'status': 'completed',
        'message': f'Sandbox bank withdrawal of KES {amount} processed instantly (no real transfer).',
    }
