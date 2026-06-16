"""
settlement.py — KWallet pool reconciliation.
Risk #04: orphaned transaction cleanup.
Risk #12: insolvency alert via email + logging (not just log file).
"""
import logging
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction as db_transaction

logger = logging.getLogger(__name__)


def reconcile_pool():
    """
    Compare wallet sum vs CompanyAccount balance.
    Risk #12: fire alert on insolvency, not just log to file.
    """
    from .models import CurrencyBalance, CompanyAccount, PoolLedger

    results = {}
    currencies = CurrencyBalance.objects.values_list('currency', flat=True).distinct()

    for curr in currencies:
        from django.db.models import Sum
        wallet_sum = CurrencyBalance.objects.filter(currency=curr).aggregate(
            total=Sum('balance')
        )['total'] or Decimal('0')

        company, _ = CompanyAccount.objects.get_or_create(
            currency=curr, defaults={'balance': Decimal('0')}
        )
        pool_balance = company.balance
        delta = pool_balance - wallet_sum

        verdict = 'SOLVENT' if pool_balance >= wallet_sum else 'INSOLVENT'
        results[curr] = {
            'wallet_sum': float(wallet_sum),
            'pool_balance': float(pool_balance),
            'delta': float(delta),
            'verdict': verdict,
        }

        if verdict == 'INSOLVENT':
            # Risk #12: alert immediately — not just a log file
            logger.critical(
                f'INSOLVENCY: {curr} pool={pool_balance} wallets={wallet_sum} delta={delta}'
            )
            try:
                from django.core.mail import mail_admins
                mail_admins(
                    subject=f'[CRITICAL] KWallet {curr} INSOLVENT',
                    message=(
                        f'Pool balance for {curr}: {pool_balance}\n'
                        f'Sum of wallet balances: {wallet_sum}\n'
                        f'Shortfall: {abs(delta)}\n\n'
                        f'Immediate investigation required.'
                    ),
                    fail_silently=True,
                )
            except Exception as e:
                logger.exception(f'Failed to send insolvency alert: {e}')
        else:
            logger.info(f'Pool OK: {curr} pool={pool_balance} wallets={wallet_sum} delta={delta}')

    return results


def resolve_orphaned_transactions(dry_run=False):
    """
    Risk #04: auto-refund pending transactions that have timed out.
    Called by cron every 30 minutes.
    """
    from .models import MpesaTransaction, AirtelTransaction, BankTransaction, Wallet
    from .views import _refund_balance

    now = timezone.now()
    refunded = []

    # M-Pesa pending withdrawals
    stale_mpesa = MpesaTransaction.objects.filter(
        status='pending',
        transaction_type='mpesa_withdraw',
        timeout_at__lte=now,
    )
    for txn in stale_mpesa:
        logger.warning(f'Orphaned M-Pesa withdrawal {txn.checkout_request_id} — auto-refunding KES {txn.amount}')
        if not dry_run:
            with db_transaction.atomic():
                txn.status = 'failed'
                txn.save()
                _refund_balance(txn.wallet, 'KES', txn.amount, txn.checkout_request_id)
        refunded.append(('mpesa_withdraw', str(txn.checkout_request_id), float(txn.amount)))

    # Airtel pending withdrawals
    stale_airtel = AirtelTransaction.objects.filter(
        status='pending',
        transaction_type='airtel_withdraw',
        timeout_at__lte=now,
    )
    for txn in stale_airtel:
        logger.warning(f'Orphaned Airtel withdrawal {txn.airtel_ref} — auto-refunding KES {txn.amount}')
        if not dry_run:
            with db_transaction.atomic():
                txn.status = 'failed'
                txn.save()
                _refund_balance(txn.wallet, 'KES', txn.amount, txn.airtel_ref)
        refunded.append(('airtel_withdraw', txn.airtel_ref, float(txn.amount)))

    # Bank pending withdrawals
    stale_bank = BankTransaction.objects.filter(
        status='pending',
        transaction_type='bank_withdraw',
        timeout_at__lte=now,
    )
    for txn in stale_bank:
        logger.warning(f'Orphaned bank withdrawal {txn.pesalink_ref} — auto-refunding KES {txn.amount}')
        if not dry_run:
            with db_transaction.atomic():
                txn.status = 'failed'
                txn.save()
                _refund_balance(txn.wallet, 'KES', txn.amount, txn.pesalink_ref)
        refunded.append(('bank_withdraw', txn.pesalink_ref, float(txn.amount)))

    logger.info(f'Orphaned transaction resolution: {len(refunded)} transactions {"(dry run)" if dry_run else "refunded"}')
    return refunded
