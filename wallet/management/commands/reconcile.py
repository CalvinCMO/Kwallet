"""
Management command: reconcile
==============================
Compares KWallet's internal ledger (CurrencyBalance rows, i.e. what we owe
users) against CompanyAccount.balance (our record of real pooled money) for
each currency, using wallet.settlement.reconcile_pool().

Usage:
    # Reconcile every currency that has CurrencyBalance rows
    python manage.py reconcile

    # Exit with non-zero status if any currency is INSOLVENT (for alerting/CI)
    python manage.py reconcile --strict

Verdicts (per currency, from settlement.reconcile_pool):
  SOLVENT   — CompanyAccount.balance >= sum of user CurrencyBalance rows
  INSOLVENT — CompanyAccount.balance <  sum of user CurrencyBalance rows
              (real money held is less than what users are owed)

Recommended frequency: daily, and after resolve_orphans runs.

Example crontab:
    0 6 * * * /path/to/venv/bin/python /path/to/manage.py reconcile --strict >> /var/log/kwallet/reconcile.log 2>&1
"""

import sys
from django.core.management.base import BaseCommand
from django.utils import timezone

from wallet.settlement import reconcile_pool


class Command(BaseCommand):
    help = 'Reconcile internal CurrencyBalance ledger against CompanyAccount pool balances.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--strict', action='store_true',
            help='Exit with code 1 if any currency is INSOLVENT (for alerting/CI).',
        )

    def handle(self, *args, **options):
        started = timezone.now().strftime('%Y-%m-%d %H:%M:%S %Z')

        self.stdout.write(self.style.HTTP_INFO(
            f"\n{'='*65}\n  KWallet Reconciliation Report — {started}\n{'='*65}"
        ))

        results = reconcile_pool()

        if not results:
            self.stdout.write(self.style.WARNING(
                "\nNo currencies with balances found — nothing to reconcile."
            ))
            return

        any_problem = False

        for currency, data in results.items():
            verdict = data['verdict']
            if verdict == 'INSOLVENT':
                style = self.style.ERROR
                any_problem = True
            else:
                style = self.style.SUCCESS

            self.stdout.write(f"\n  {currency}")
            self.stdout.write(f"  {'─'*50}")
            self.stdout.write(f"  Pool balance     : {data['pool_balance']:>18,.4f} {currency}")
            self.stdout.write(f"  User liability (Σ): {data['wallet_sum']:>18,.4f} {currency}")
            self.stdout.write(f"  Delta            : {data['delta']:>18,.4f} {currency}")
            self.stdout.write(style(f"\n  {verdict}"))

        self.stdout.write(f"\n{'='*65}")
        if any_problem:
            self.stdout.write(self.style.ERROR(
                "  🔴 One or more currencies are INSOLVENT. Immediate review required."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "  All currencies reconciled successfully — pool covers user liabilities."
            ))
        self.stdout.write(f"{'='*65}\n")

        if options['strict'] and any_problem:
            sys.exit(1)
