"""
settlement.py — KWallet v2 Real-Money Settlement Engine
=========================================================
This module is the bridge between KWallet's internal ledger (database rows)
and the real-world accounts that back it.

Three public entry points:

  sweep_fees(currency, initiated_by='system')
      Collects all unsettled FeeRecord rows for a currency, writes a
      FeeSettlement, debits the client float CompanyAccount, credits the
      company revenue CompanyAccount, and marks FeeRecords as settled.
      Returns a FeeSettlement instance.

  reconcile(currency)
      Compares the sum of all CurrencyBalance rows against the client float
      CompanyAccount.ledger_balance.  Returns a ReconciliationResult
      namedtuple with the gap and a health verdict.

  pool_status()
      Returns a list of PoolStatusRow namedtuples — one per active
      CompanyAccount — summarising balance, liability, surplus, and solvency.

Design principles:
  - Every real-money movement writes an immutable PoolLedger row.
  - CompanyAccount.ledger_balance is always updated inside the same
    db_transaction.atomic() block as the PoolLedger write, so they are
    always consistent.
  - FeeRecord rows are linked to their FeeSettlement once swept; unsettled
    rows have settlement=None.
  - No real M-Pesa/bank API calls are made here — this is purely the
    internal accounting layer.  When you add real bank API calls, they go
    in a separate layer that calls sweep_fees() after confirming the real
    transfer succeeded.
"""

import logging
from collections import namedtuple
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Return types ──────────────────────────────────────────────────────────────

ReconciliationResult = namedtuple('ReconciliationResult', [
    'currency',
    'ledger_balance',       # CompanyAccount.ledger_balance
    'user_liability',       # Sum of all CurrencyBalance rows
    'unsettled_fees',       # Sum of FeeRecord rows not yet swept
    'gap',                  # ledger_balance - user_liability (should be >= 0)
    'is_solvent',           # gap >= 0
    'verdict',              # human-readable string
])

PoolStatusRow = namedtuple('PoolStatusRow', [
    'account_name',
    'account_type',
    'currency',
    'ledger_balance',
    'user_liability',       # only meaningful for client_float accounts
    'surplus',
    'is_solvent',
    'unsettled_fees',       # only meaningful for client_float accounts
])

SweepResult = namedtuple('SweepResult', [
    'settlement',           # FeeSettlement instance (or None if nothing to sweep)
    'swept_amount',         # Decimal — total fees moved
    'fee_count',            # int — number of FeeRecord rows settled
    'currency',
    'status',               # 'completed' | 'skipped' | 'failed'
    'message',
])


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_client_float(currency: str):
    """
    Returns the active client_float CompanyAccount for a currency.
    Raises ValueError if none is configured — operator must set one up first.
    """
    from .models import CompanyAccount
    try:
        return CompanyAccount.objects.get(
            currency=currency,
            account_type='client_float',
            is_active=True,
        )
    except CompanyAccount.DoesNotExist:
        raise ValueError(
            f"No active client_float CompanyAccount configured for {currency}. "
            f"Create one in the admin panel before running sweeps or reconciliation."
        )
    except CompanyAccount.MultipleObjectsReturned:
        raise ValueError(
            f"Multiple active client_float CompanyAccounts found for {currency}. "
            f"Deactivate duplicates in the admin panel."
        )


def _get_revenue_account(currency: str):
    """
    Returns the active company_revenue CompanyAccount for a currency.
    """
    from .models import CompanyAccount
    try:
        return CompanyAccount.objects.get(
            currency=currency,
            account_type='company_revenue',
            is_active=True,
        )
    except CompanyAccount.DoesNotExist:
        raise ValueError(
            f"No active company_revenue CompanyAccount configured for {currency}. "
            f"Create one in the admin panel before running fee sweeps."
        )
    except CompanyAccount.MultipleObjectsReturned:
        raise ValueError(
            f"Multiple active company_revenue CompanyAccounts found for {currency}. "
            f"Deactivate duplicates in the admin panel."
        )


def _write_pool_entry(account, entry_type, amount, currency,
                      transaction=None, settlement=None,
                      note='', created_by='system'):
    """
    Writes a PoolLedger row and updates CompanyAccount.ledger_balance atomically.
    Must be called inside a db_transaction.atomic() block.
    Returns the PoolLedger instance.
    """
    from .models import PoolLedger

    # Determine sign: 'in' entries increase balance, 'out' entries decrease it
    IN_TYPES  = {'deposit_in', 'fee_sweep_in', 'fx_rebalance_in', 'adjustment'}
    OUT_TYPES = {'withdrawal_out', 'fee_sweep_out', 'fx_rebalance_out'}

    if entry_type in IN_TYPES:
        account.ledger_balance += amount
    elif entry_type in OUT_TYPES:
        account.ledger_balance -= amount
    else:
        raise ValueError(f"Unknown entry_type: {entry_type}")

    account.ledger_balance = account.ledger_balance.quantize(Decimal('0.0001'))
    account.save(update_fields=['ledger_balance', 'updated_at'])

    entry = PoolLedger.objects.create(
        account=account,
        entry_type=entry_type,
        amount=amount.quantize(Decimal('0.0001')),
        currency=currency,
        balance_after=account.ledger_balance,
        transaction=transaction,
        settlement=settlement,
        note=note,
        created_by=created_by,
    )
    return entry


# ── Public API ────────────────────────────────────────────────────────────────

def record_deposit(transaction_obj, created_by='system'):
    """
    Called immediately after a deposit is confirmed (e.g. from the M-Pesa
    callback handler).  Writes a deposit_in PoolLedger entry against the
    client float account for the transaction's currency.

    This keeps CompanyAccount.ledger_balance in sync with real deposits
    as they arrive, not just during reconciliation.
    """
    currency = transaction_obj.currency
    amount   = transaction_obj.amount

    try:
        float_account = _get_client_float(currency)
    except ValueError as e:
        logger.warning(f"record_deposit skipped — {e}")
        return None

    with db_transaction.atomic():
        entry = _write_pool_entry(
            account=float_account,
            entry_type='deposit_in',
            amount=amount,
            currency=currency,
            transaction=transaction_obj,
            note=f"Deposit confirmed. Tx ref: {transaction_obj.reference}",
            created_by=created_by,
        )
    logger.info(
        f"record_deposit | {amount} {currency} → {float_account.name} "
        f"| new balance: {float_account.ledger_balance}"
    )
    return entry


def record_withdrawal(transaction_obj, created_by='system'):
    """
    Called immediately after a withdrawal is confirmed (B2C result callback).
    Writes a withdrawal_out PoolLedger entry against the client float account.
    """
    currency = transaction_obj.currency
    amount   = transaction_obj.amount

    try:
        float_account = _get_client_float(currency)
    except ValueError as e:
        logger.warning(f"record_withdrawal skipped — {e}")
        return None

    with db_transaction.atomic():
        entry = _write_pool_entry(
            account=float_account,
            entry_type='withdrawal_out',
            amount=amount,
            currency=currency,
            transaction=transaction_obj,
            note=f"Withdrawal confirmed. Tx ref: {transaction_obj.reference}",
            created_by=created_by,
        )
    logger.info(
        f"record_withdrawal | {amount} {currency} ← {float_account.name} "
        f"| new balance: {float_account.ledger_balance}"
    )
    return entry


def sweep_fees(currency: str, initiated_by: str = 'system') -> SweepResult:
    """
    Sweeps all unsettled fee records for `currency` from the client float
    account to the company revenue account.

    Steps:
      1. Find all FeeRecord rows for this currency with settlement=None
      2. Sum them up
      3. Create a FeeSettlement (status=pending)
      4. In a single atomic block:
           a. Debit client float  (fee_sweep_out PoolLedger entry)
           b. Credit company revenue (fee_sweep_in PoolLedger entry)
           c. Mark all FeeRecord rows with this settlement
           d. Mark FeeSettlement as completed
      5. Return a SweepResult

    If the client float would go negative after the sweep, the sweep is
    aborted and a failed SweepResult is returned.
    """
    from .models import FeeRecord, FeeSettlement

    # ── 1. Gather unsettled fees ──────────────────────────────────────────────
    unsettled_qs = FeeRecord.objects.filter(
        currency=currency,
        settlement__isnull=True,
    )
    agg = unsettled_qs.aggregate(total=Sum('amount'), count=models.Count('id'))
    total_fees = (agg['total'] or Decimal('0')).quantize(Decimal('0.0001'))
    fee_count  = agg['count'] or 0

    if total_fees <= Decimal('0') or fee_count == 0:
        logger.info(f"sweep_fees({currency}): nothing to sweep.")
        return SweepResult(
            settlement=None, swept_amount=Decimal('0'),
            fee_count=0, currency=currency,
            status='skipped', message='No unsettled fees to sweep.',
        )

    # ── 2. Get accounts ───────────────────────────────────────────────────────
    try:
        float_account   = _get_client_float(currency)
        revenue_account = _get_revenue_account(currency)
    except ValueError as e:
        logger.error(f"sweep_fees({currency}) account config error: {e}")
        return SweepResult(
            settlement=None, swept_amount=Decimal('0'),
            fee_count=0, currency=currency,
            status='failed', message=str(e),
        )

    # ── 3. Solvency pre-check ─────────────────────────────────────────────────
    # After sweeping, float balance must still cover all user liabilities
    projected_float = float_account.ledger_balance - total_fees
    if projected_float < float_account.total_user_liability:
        msg = (
            f"Sweep aborted — post-sweep float ({projected_float} {currency}) "
            f"would fall below user liability "
            f"({float_account.total_user_liability} {currency}). "
            f"Reconcile and top up the float account first."
        )
        logger.error(f"sweep_fees({currency}): {msg}")
        return SweepResult(
            settlement=None, swept_amount=Decimal('0'),
            fee_count=0, currency=currency,
            status='failed', message=msg,
        )

    # ── 4. Create settlement record (pending) ─────────────────────────────────
    settlement = FeeSettlement.objects.create(
        currency=currency,
        total_fees=total_fees,
        fee_count=fee_count,
        from_account=float_account,
        to_account=revenue_account,
        status='pending',
        initiated_by=initiated_by,
    )

    # ── 5. Atomic sweep ───────────────────────────────────────────────────────
    try:
        with db_transaction.atomic():
            # Debit client float
            _write_pool_entry(
                account=float_account,
                entry_type='fee_sweep_out',
                amount=total_fees,
                currency=currency,
                settlement=settlement,
                note=f"Fee sweep {settlement.reference} — {fee_count} fees",
                created_by=initiated_by,
            )
            # Credit company revenue
            _write_pool_entry(
                account=revenue_account,
                entry_type='fee_sweep_in',
                amount=total_fees,
                currency=currency,
                settlement=settlement,
                note=f"Fee sweep {settlement.reference} — {fee_count} fees",
                created_by=initiated_by,
            )
            # Link FeeRecord rows to this settlement
            unsettled_qs.update(settlement=settlement)

            # Mark settlement complete
            settlement.status       = 'completed'
            settlement.completed_at = timezone.now()
            settlement.save(update_fields=['status', 'completed_at'])

        logger.info(
            f"sweep_fees({currency}): swept {total_fees} {currency} "
            f"({fee_count} fees) | ref: {settlement.reference}"
        )
        return SweepResult(
            settlement=settlement,
            swept_amount=total_fees,
            fee_count=fee_count,
            currency=currency,
            status='completed',
            message=(
                f"Swept {total_fees} {currency} ({fee_count} fees) "
                f"→ {revenue_account.name}. Ref: {settlement.reference}"
            ),
        )

    except Exception as exc:
        settlement.status         = 'failed'
        settlement.failure_reason = str(exc)
        settlement.save(update_fields=['status', 'failure_reason'])
        logger.exception(f"sweep_fees({currency}) failed: {exc}")
        return SweepResult(
            settlement=settlement, swept_amount=Decimal('0'),
            fee_count=0, currency=currency,
            status='failed', message=str(exc),
        )


def reconcile(currency: str) -> ReconciliationResult:
    """
    Compares the internal ledger against real-world account balances.

    What it checks:
      ledger_balance   = CompanyAccount.ledger_balance (our internal view of
                         what sits in the real M-Pesa / bank account)
      user_liability   = sum of all CurrencyBalance rows for this currency
                         (what we owe users collectively)
      gap              = ledger_balance - user_liability
                         Must be >= 0 at all times.
                         The gap represents unsettled fees sitting in the pool.
      unsettled_fees   = sum of FeeRecord rows not yet swept
                         Should roughly equal the gap.

    Verdicts:
      HEALTHY   — gap >= 0 and gap ≈ unsettled_fees (within 1%)
      WARNING   — gap >= 0 but gap differs from unsettled_fees by > 1%
                  (suggests a deposit or withdrawal wasn't recorded in pool)
      INSOLVENT — gap < 0 — CRITICAL: user liabilities exceed real money held
    """
    from .models import FeeRecord, CurrencyBalance

    try:
        float_account = _get_client_float(currency)
    except ValueError as e:
        return ReconciliationResult(
            currency=currency,
            ledger_balance=Decimal('0'),
            user_liability=Decimal('0'),
            unsettled_fees=Decimal('0'),
            gap=Decimal('0'),
            is_solvent=False,
            verdict=f"CONFIG ERROR — {e}",
        )

    ledger_balance = float_account.ledger_balance
    user_liability = float_account.total_user_liability

    unsettled_agg  = FeeRecord.objects.filter(
        currency=currency, settlement__isnull=True
    ).aggregate(total=Sum('amount'))
    unsettled_fees = (unsettled_agg['total'] or Decimal('0')).quantize(Decimal('0.0001'))

    gap = (ledger_balance - user_liability).quantize(Decimal('0.0001'))

    if gap < Decimal('0'):
        verdict = (
            f"🔴 INSOLVENT — pool is short {abs(gap)} {currency}. "
            f"User liabilities ({user_liability}) exceed pool ({ledger_balance}). "
            f"Immediate action required."
        )
        is_solvent = False
    else:
        # Tolerance: gap should be within 1% of unsettled_fees
        tolerance = (unsettled_fees * Decimal('0.01')).quantize(Decimal('0.0001'))
        diff = abs(gap - unsettled_fees)
        if diff <= max(tolerance, Decimal('1')):
            verdict = (
                f"✅ HEALTHY — pool surplus {gap} {currency} "
                f"matches unsettled fees {unsettled_fees} {currency}."
            )
        else:
            verdict = (
                f"🟡 WARNING — pool surplus ({gap} {currency}) differs from "
                f"unsettled fees ({unsettled_fees} {currency}) by {diff} {currency}. "
                f"A deposit or withdrawal may not be reflected in the pool ledger. "
                f"Check PoolLedger entries."
            )
        is_solvent = True

    return ReconciliationResult(
        currency=currency,
        ledger_balance=ledger_balance,
        user_liability=user_liability,
        unsettled_fees=unsettled_fees,
        gap=gap,
        is_solvent=is_solvent,
        verdict=verdict,
    )


def pool_status() -> list:
    """
    Returns a PoolStatusRow for every active CompanyAccount.
    Used by the pool_status management command and admin dashboard.
    """
    from .models import CompanyAccount, FeeRecord
    from django.db.models import Sum as DSum

    rows = []
    for account in CompanyAccount.objects.filter(is_active=True):
        if account.account_type == 'client_float':
            user_liability = account.total_user_liability
            surplus        = account.surplus
            is_solvent     = account.is_solvent
            fee_agg        = FeeRecord.objects.filter(
                currency=account.currency, settlement__isnull=True
            ).aggregate(total=DSum('amount'))
            unsettled = (fee_agg['total'] or Decimal('0')).quantize(Decimal('0.0001'))
        else:
            user_liability = Decimal('0')
            surplus        = account.ledger_balance
            is_solvent     = True
            unsettled      = Decimal('0')

        rows.append(PoolStatusRow(
            account_name=account.name,
            account_type=account.get_account_type_display(),
            currency=account.currency,
            ledger_balance=account.ledger_balance,
            user_liability=user_liability,
            surplus=surplus,
            is_solvent=is_solvent,
            unsettled_fees=unsettled,
        ))
    return rows


# ── Missing import fix ────────────────────────────────────────────────────────
# django.db.models.Count used in sweep_fees aggregate above
from django.db import models
