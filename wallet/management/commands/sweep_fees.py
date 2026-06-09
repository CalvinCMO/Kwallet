"""
Management command: sweep_fees
==============================
Sweeps accumulated unsettled fees from the client float account into the
company revenue account for one or more currencies.

Usage:
    # Sweep a single currency
    python manage.py sweep_fees --currency KES

    # Sweep all currencies that have active CompanyAccounts
    python manage.py sweep_fees --all

    # Dry run — shows what would be swept without writing anything
    python manage.py sweep_fees --all --dry-run

Intended to be run on a schedule (cron, Celery beat, etc.).
Recommended frequency: once per day, e.g. 02:00 Africa/Nairobi.

Example crontab:
    0 2 * * * /path/to/venv/bin/python /path/to/manage.py sweep_fees --all >> /var/log/kwallet/sweep.log 2>&1
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from wallet.settlement import sweep_fees
from wallet.models import CompanyAccount


class Command(BaseCommand):
    help = 'Sweep accumulated fees from client float → company revenue account.'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            '--currency', type=str, metavar='CUR',
            help='3-letter currency code to sweep (e.g. KES, USD).',
        )
        group.add_argument(
            '--all', action='store_true',
            help='Sweep all currencies that have active client_float accounts.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would be swept without writing anything to the database.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        started = timezone.now().strftime('%Y-%m-%d %H:%M:%S %Z')

        self.stdout.write(self.style.HTTP_INFO(
            f"\n{'='*60}\n  KWallet Fee Sweep — {started}\n{'='*60}"
        ))

        if dry_run:
            self.stdout.write(self.style.WARNING("  ⚠️  DRY RUN — no changes will be written.\n"))

        # Build list of currencies to process
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

        total_swept  = 0
        total_fees   = 0
        skipped      = 0
        failed       = 0

        for currency in currencies:
            self.stdout.write(f"\n  Processing {currency}…")

            if dry_run:
                # In dry-run mode, just show what exists without sweeping
                from django.db.models import Sum, Count
                from wallet.models import FeeRecord
                agg = FeeRecord.objects.filter(
                    currency=currency, settlement__isnull=True
                ).aggregate(total=Sum('amount'), count=Count('id'))
                amount = agg['total'] or 0
                count  = agg['count'] or 0
                if count == 0:
                    self.stdout.write(f"    → Nothing to sweep.")
                else:
                    self.stdout.write(self.style.WARNING(
                        f"    → Would sweep {amount:.4f} {currency} ({count} fee records)."
                    ))
                continue

            result = sweep_fees(currency=currency, initiated_by='manage_sweep_fees')

            if result.status == 'completed':
                total_swept += 1
                total_fees  += result.fee_count
                self.stdout.write(self.style.SUCCESS(
                    f"    ✅ Swept {result.swept_amount:.4f} {currency} "
                    f"({result.fee_count} fees). "
                    f"Ref: {result.settlement.reference}"
                ))
            elif result.status == 'skipped':
                skipped += 1
                self.stdout.write(f"    ⏭  Skipped — {result.message}")
            else:  # failed
                failed += 1
                self.stdout.write(self.style.ERROR(
                    f"    ❌ FAILED — {result.message}"
                ))

        # Summary
        self.stdout.write(f"\n{'─'*60}")
        if dry_run:
            self.stdout.write(self.style.WARNING(
                "  Dry run complete. No changes made."
            ))
        else:
            self.stdout.write(
                f"  Done. Swept: {total_swept} currencies | "
                f"Fees settled: {total_fees} | "
                f"Skipped: {skipped} | Failed: {failed}"
            )
            if failed:
                self.stdout.write(self.style.ERROR(
                    "  ⚠️  Some currencies failed. Check logs above."
                ))
        self.stdout.write(f"{'='*60}\n")
