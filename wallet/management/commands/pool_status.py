"""
Management command: pool_status
================================
Prints a real-time snapshot of every CompanyAccount — both client float
accounts and company revenue accounts — showing balances, liabilities,
surpluses, and solvency health.

Usage:
    python manage.py pool_status

    # Only show accounts that have a problem
    python manage.py pool_status --problems-only

Output columns (client float accounts):
  Account        — CompanyAccount.name
  Currency       — 3-letter code
  Pool Balance   — CompanyAccount.ledger_balance (real money we hold)
  User Liability — Sum of CurrencyBalance rows (what we owe users)
  Surplus        — Pool Balance − User Liability (must be ≥ 0)
  Unsettled Fees — Fees collected but not yet swept to revenue account
  Status         — ✅ SOLVENT | 🔴 INSOLVENT

Output columns (company revenue accounts):
  Account        — CompanyAccount.name
  Currency       — 3-letter code
  Balance        — Total revenue collected and swept in
  Status         — always ✅ (revenue accounts can't be insolvent)
"""

from django.core.management.base import BaseCommand

from wallet.settlement import pool_status


class Command(BaseCommand):
    help = 'Show real-time snapshot of all CompanyAccount balances and pool health.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--problems-only', action='store_true',
            help='Only print accounts that are insolvent or have a warning.',
        )

    def handle(self, *args, **options):
        problems_only = options['problems_only']
        rows = pool_status()

        if not rows:
            self.stdout.write(self.style.WARNING(
                "\nNo active CompanyAccounts found.\n"
                "Create them in the Django admin panel:\n"
                "  1. Go to /admin/wallet/companyaccount/add/\n"
                "  2. Add a 'Client Float' account for each currency you hold\n"
                "  3. Add a 'Company Revenue' account for each currency\n"
            ))
            return

        # ── Client Float section ───────────────────────────────────────────────
        float_rows   = [r for r in rows if 'Client Float' in r.account_type]
        revenue_rows = [r for r in rows if 'Revenue' in r.account_type]

        self.stdout.write(self.style.HTTP_INFO(
            f"\n{'='*80}\n  KWallet Pool Status\n{'='*80}"
        ))

        if float_rows:
            self.stdout.write(self.style.HTTP_INFO("\n  CLIENT FLOAT ACCOUNTS (Segregated User Funds)"))
            self.stdout.write(f"  {'─'*76}")
            header = (
                f"  {'Account':<28} {'CCY':>4}  "
                f"{'Pool Balance':>14}  {'User Liability':>14}  "
                f"{'Surplus':>12}  {'Unsettled Fees':>14}  {'Status':>10}"
            )
            self.stdout.write(header)
            self.stdout.write(f"  {'─'*76}")

            for r in float_rows:
                if problems_only and r.is_solvent:
                    continue

                status_str = "✅ SOLVENT" if r.is_solvent else "🔴 INSOLVENT"
                style_fn   = self.style.SUCCESS if r.is_solvent else self.style.ERROR

                # Warn if surplus deviates significantly from unsettled fees
                tolerance = r.unsettled_fees * 0.01
                deviation = abs(r.surplus - r.unsettled_fees)
                if r.is_solvent and deviation > max(tolerance, 1):
                    status_str = "🟡 WARNING"
                    style_fn   = self.style.WARNING

                line = (
                    f"  {r.account_name:<28} {r.currency:>4}  "
                    f"{r.ledger_balance:>14.4f}  {r.user_liability:>14.4f}  "
                    f"{r.surplus:>12.4f}  {r.unsettled_fees:>14.4f}  {status_str:>10}"
                )
                self.stdout.write(style_fn(line))

        # ── Company Revenue section ────────────────────────────────────────────
        if revenue_rows:
            self.stdout.write(self.style.HTTP_INFO(
                "\n\n  COMPANY REVENUE ACCOUNTS (KWallet Operating Funds)"
            ))
            self.stdout.write(f"  {'─'*50}")
            header2 = f"  {'Account':<35} {'CCY':>4}  {'Balance':>14}"
            self.stdout.write(header2)
            self.stdout.write(f"  {'─'*50}")

            for r in revenue_rows:
                if problems_only:
                    continue
                line = f"  {r.account_name:<35} {r.currency:>4}  {r.ledger_balance:>14.4f}"
                self.stdout.write(self.style.SUCCESS(line))

        # ── Summary ───────────────────────────────────────────────────────────
        insolvent = [r for r in float_rows if not r.is_solvent]
        self.stdout.write(f"\n{'─'*80}")
        if insolvent:
            self.stdout.write(self.style.ERROR(
                f"  🔴 CRITICAL — {len(insolvent)} account(s) are INSOLVENT. "
                f"Immediate action required."
            ))
        else:
            total_unsettled = sum(r.unsettled_fees for r in float_rows)
            self.stdout.write(self.style.SUCCESS(
                f"  All float accounts solvent. "
                f"Total unsettled fees pending sweep: "
                + ", ".join(
                    f"{r.unsettled_fees:.4f} {r.currency}"
                    for r in float_rows if r.unsettled_fees > 0
                ) or "none"
            ))
        self.stdout.write(f"{'='*80}\n")

        # ── Setup hint ────────────────────────────────────────────────────────
        if not float_rows:
            self.stdout.write(self.style.WARNING(
                "  No client float accounts configured yet.\n"
                "  Go to /admin/wallet/companyaccount/ to add them.\n"
            ))
