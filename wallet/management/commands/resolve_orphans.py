"""
Risk #04 & #11: management command to auto-refund timed-out pending transactions.
Schedule via Railway cron: every 30 minutes.
"""
from django.core.management.base import BaseCommand
from wallet.settlement import resolve_orphaned_transactions, reconcile_pool


class Command(BaseCommand):
    help = 'Resolve orphaned pending transactions and reconcile pool ledger'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Preview only, no changes')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        self.stdout.write('=== Resolving orphaned transactions ===')
        refunded = resolve_orphaned_transactions(dry_run=dry_run)
        for txn_type, ref, amount in refunded:
            self.stdout.write(f'  Refunded {txn_type} {ref}: KES {amount:,.2f}')
        self.stdout.write(f'Total: {len(refunded)} transactions{"(dry run)" if dry_run else " refunded"}')

        self.stdout.write('\n=== Pool reconciliation ===')
        results = reconcile_pool()
        for curr, data in results.items():
            verdict = data['verdict']
            style   = self.style.ERROR if verdict == 'INSOLVENT' else self.style.SUCCESS
            self.stdout.write(style(
                f'  {curr}: pool={data["pool_balance"]:,.2f} wallets={data["wallet_sum"]:,.2f} '
                f'delta={data["delta"]:+,.2f} — {verdict}'
            ))
