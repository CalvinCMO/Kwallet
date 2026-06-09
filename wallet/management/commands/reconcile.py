"""
Management command: reconcile
==============================
Compares KWallet's internal ledger (CurrencyBalance rows) against the
CompanyAccount.ledger_balance for each currency.

Usage:
    # Reconcile a single currency
    python manage.py reconcile --currency KES

    # Reconcile all currencies with active float accounts
    python manage.py reconcile --all

    # Exit with non-zero status if any currency is not HEALTHY (useful for alerting)
    python manage.py reconcile --all --strict

What it checks (per currency):
  ┌─────────────────────────────────────────────────────────────┐
  │  ledger_balance   = CompanyAccount.ledger_balance           │
  │                     (our record of real money in the pool)  │
  │  user_liability   = SUM of all CurrencyBalance rows         │
  │                     (what we owe users)                     │
  │  gap              = ledger_balance - user_liability          │
  │                     Must ALWAYS be >= 0                     │
  │  unsettled_fees   = SUM of FeeRecord rows not yet swept     │
  │                     Should ≈ gap                            │
  └─────────────────────────────────────────────────────────────┘

Verdicts:
  ✅ HEALTHY   — gap >= 0 and gap ≈ unsettled fees
  🟡 WARNING   — gap >= 0 but differs from unsettled fees (un-recorded movement)
  🔴 INSOLVENT — gap < 0 — CRITICAL: real money < what users are owed

Recommended frequency: daily at 06:00, and before/after every sweep.

Example crontab:
    0 6 * * * /path/to/venv/bin/python /path/to/manage.py reconcile --all --strict >> /var/log/kwallet/reconcile.log 2>&1
"""

import sys
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from wallet.settlement import reconcile
from wallet.models import CompanyAccount


class Command(BaseCommand):
    help = 'Reconcile internal ledger balances against real-world pool accounts.'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            '--currency', type=str, metavar='CUR',
            help='3-letter currency code to reconcile.',
        )
        group.add_argument(
            '--all', action='store_true',
            help='Reconcile all currencies with active client_float accounts.',
        )
        parser.add_argument(
            '--strict', action='store_true',
            help='Exit with code 1 if any currency is not HEALTHY (for alerting/CI).',
        )

    def handle(self, *args, **options):
        started = timezone.now().strftime('%Y-%m-%d %H:%M:%S %Z')

        self.stdout.write(self.style.HTTP_INFO(
            f"\n{'='*65}\n  KWallet Reconciliation Report — {started}\n{'='*65}"
        ))

        # Build currency list
        if options['all']:
            currencies = list(
                CompanyAccount.objects.filter(
                    account_type='client_float', is_active=True
                ).values_list('currency', flat=True).distinct()
            )
            if not currencies:
                raise CommandError(
                    "No active client_float CompanyAccounts found. "
                    "Create them in the admin panel first."
                )
        else:
            currencies = [options['currency'].upper()]

        any_problem = False

        for currency in currencies:
            result = reconcile(currency)

            # Determine display style based on verdict prefix
            if '🔴' in result.verdict:
                style    = self.style.ERROR
                any_problem = True
            elif '🟡' in result.verdict:
                style    = self.style.WARNING
                any_problem = True
            else:
                style    = self.style.SUCCESS

            self.stdout.write(f"\n  {currency}")
            self.stdout.write(f"  {'─'*50}")
            self.stdout.write(
                f"  Pool ledger balance : {result.ledger_balance:>18.4f} {currency}"
            )
            self.stdout.write(
                f"  User liability (Σ)  : {result.user_liability:>18.4f} {currency}"
            )
            self.stdout.write(
                f"  Gap (surplus)       : {result.gap:>18.4f} {currency}"
            )
            self.stdout.write(
                f"  Unsettled fees      : {result.unsettled_fees:>18.4f} {currency}"
            )
            self.stdout.write(style(f"\n  {result.verdict}"))

        self.stdout.write(f"\n{'='*65}")
        if any_problem:
            self.stdout.write(self.style.WARNING(
                "  ⚠️  One or more currencies need attention. Review above."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "  All currencies reconciled successfully."
            ))
        self.stdout.write(f"{'='*65}\n")

        if options['strict'] and any_problem:
            sys.exit(1)
