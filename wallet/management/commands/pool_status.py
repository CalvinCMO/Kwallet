"""
Management command: pool_status
================================
Prints a real-time snapshot of every CompanyAccount currency pool —
pool balance vs. user liability (sum of CurrencyBalance rows) — using
wallet.settlement.reconcile_pool().

Usage:
    python manage.py pool_status

    # Only show currencies that are INSOLVENT
    python manage.py pool_status --problems-only

NOTE: the current CompanyAccount model holds a single pooled balance per
currency (no separate "client float" vs "revenue" account split). This
command reports against that single-balance-per-currency model.
"""

from django.core.management.base import BaseCommand

from wallet.settlement import reconcile_pool


class Command(BaseCommand):
    help = 'Show real-time snapshot of all CompanyAccount pool balances vs. user liabilities.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--problems-only', action='store_true',
            help='Only print currencies that are INSOLVENT.',
        )

    def handle(self, *args, **options):
        problems_only = options['problems_only']
        results = reconcile_pool()

        if not results:
            self.stdout.write(self.style.WARNING(
                "\nNo currencies with balances found.\n"
                "CompanyAccount rows are created automatically as deposits/withdrawals occur.\n"
            ))
            return

        self.stdout.write(self.style.HTTP_INFO(
            f"\n{'='*72}\n  KWallet Pool Status\n{'='*72}"
        ))
        header = f"  {'Currency':<10} {'Pool Balance':>16}  {'User Liability':>16}  {'Delta':>14}  {'Status':>10}"
        self.stdout.write(header)
        self.stdout.write(f"  {'─'*68}")

        insolvent = []
        for currency, data in results.items():
            is_solvent = data['verdict'] == 'SOLVENT'
            if problems_only and is_solvent:
                continue
            if not is_solvent:
                insolvent.append(currency)

            status_str = "✅ SOLVENT" if is_solvent else "🔴 INSOLVENT"
            style_fn   = self.style.SUCCESS if is_solvent else self.style.ERROR
            line = (
                f"  {currency:<10} {data['pool_balance']:>16,.4f}  "
                f"{data['wallet_sum']:>16,.4f}  {data['delta']:>14,.4f}  {status_str:>10}"
            )
            self.stdout.write(style_fn(line))

        self.stdout.write(f"\n{'─'*72}")
        if insolvent:
            self.stdout.write(self.style.ERROR(
                f"  🔴 CRITICAL — {len(insolvent)} currency(ies) INSOLVENT: {', '.join(insolvent)}. "
                f"Immediate action required."
            ))
        else:
            self.stdout.write(self.style.SUCCESS("  All currency pools solvent."))
        self.stdout.write(f"{'='*72}\n")
