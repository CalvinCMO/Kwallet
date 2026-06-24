"""
Migration: progressive limit tiers on WalletLimit

Changes:
  - daily_withdraw_kes  default 70000 → 10000  (Tier 0 new unverified)
  - per_txn_max_kes     default 150000 → 10000
  - monthly_limit_kes   default 1000000 → 300000
  - Add tier_override   (nullable SmallIntegerField, admin pin)
  - Add last_tier_update (nullable DateTimeField)

Existing rows are left as-is — their limits are now computed dynamically
via Wallet.get_effective_limits(); the stored values are only a cache /
snapshot that sync_from_tier() refreshes.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0004_wallet_is_sandbox'),
    ]

    operations = [
        # Update defaults (existing rows keep their current values)
        migrations.AlterField(
            model_name='walletlimit',
            name='daily_withdraw_kes',
            field=models.DecimalField(
                max_digits=12, decimal_places=2, default=10000,
                help_text='Cached daily withdrawal cap (KES). Auto-computed from tier; do not edit manually.'
            ),
        ),
        migrations.AlterField(
            model_name='walletlimit',
            name='per_txn_max_kes',
            field=models.DecimalField(
                max_digits=12, decimal_places=2, default=10000,
                help_text='Cached per-transaction cap (KES).'
            ),
        ),
        migrations.AlterField(
            model_name='walletlimit',
            name='monthly_limit_kes',
            field=models.DecimalField(
                max_digits=12, decimal_places=2, default=300000,
                help_text='Cached monthly withdrawal cap (KES).'
            ),
        ),
        # New fields
        migrations.AddField(
            model_name='walletlimit',
            name='tier_override',
            field=models.SmallIntegerField(
                null=True, blank=True,
                choices=[
                    (0, 'Tier 0 — New Unverified'),
                    (1, 'Tier 1 — Established Unverified'),
                    (2, 'Tier 2 — KYC Verified'),
                    (3, 'Tier 3 — Fully Verified'),
                ],
                help_text='Pin this wallet to a specific tier. Leave blank for automatic progression.'
            ),
        ),
        migrations.AddField(
            model_name='walletlimit',
            name='last_tier_update',
            field=models.DateTimeField(
                null=True, blank=True,
                help_text='Timestamp of last sync_from_tier() call.'
            ),
        ),
    ]
