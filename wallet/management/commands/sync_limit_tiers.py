"""
Management command: sync_limit_tiers

Recomputes and saves the progressive withdrawal limits for every wallet
based on their current tier eligibility.  Run this via cron (e.g. nightly)
so that WalletLimit cached values stay accurate.

Usage:
    python manage.py sync_limit_tiers
    python manage.py sync_limit_tiers --wallet KW1A2B3C4D5E   # single wallet
    python manage.py sync_limit_tiers --dry-run               # preview only
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from wallet.models import Wallet, WalletLimit, LIMIT_TIERS


class Command(BaseCommand):
    help = 'Sync WalletLimit cached values from each wallet\'s current progressive tier.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--wallet', dest='wallet_id', default=None,
            help='Only sync a single wallet by wallet_id.'
        )
        parser.add_argument(
            '--dry-run', action='store_true', default=False,
            help='Print changes without saving.'
        )

    def handle(self, *args, **options):
        qs = Wallet.objects.select_related('limit').all()
        if options['wallet_id']:
            qs = qs.filter(wallet_id=options['wallet_id'])

        dry_run = options['dry_run']
        updated = 0
        unchanged = 0

        for wallet in qs:
            limit, created = WalletLimit.objects.get_or_create(wallet=wallet)
            tier   = (
                limit.tier_override
                if limit.tier_override is not None
                else wallet.get_limit_tier()
            )
            eff    = wallet.get_effective_limits()
            new_d  = eff['daily']
            new_pt = eff['per_txn']
            new_m  = eff['monthly']

            changed = (
                float(limit.daily_withdraw_kes) != new_d or
                float(limit.per_txn_max_kes)    != new_pt or
                float(limit.monthly_limit_kes)   != new_m
            )

            tier_label = LIMIT_TIERS[tier]['label']

            if changed:
                self.stdout.write(
                    f'  {wallet.wallet_id}  tier={tier} ({tier_label})\n'
                    f'    daily  {float(limit.daily_withdraw_kes):>12,.0f} → {new_d:>12,.0f}\n'
                    f'    per_txn{float(limit.per_txn_max_kes):>12,.0f} → {new_pt:>12,.0f}\n'
                    f'    monthly{float(limit.monthly_limit_kes):>12,.0f} → {new_m:>12,.0f}'
                )
                if not dry_run:
                    limit.sync_from_tier()
                updated += 1
            else:
                unchanged += 1

        verb = 'Would update' if dry_run else 'Updated'
        self.stdout.write(
            self.style.SUCCESS(
                f'\n{verb} {updated} wallet(s); {unchanged} already current.'
            )
        )
