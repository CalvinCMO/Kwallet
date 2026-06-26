"""
Migration: add FlutterwaveTransaction model and FLW transaction type choices.

Creates the FlutterwaveTransaction table which tracks every payment and
payout initiated via the Flutterwave integration (card, bank transfer,
mobile money deposits, bank/mobile payouts).
"""
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0005_walletlimit_progressive_tiers'),
    ]

    operations = [
        # Update TRANSACTION_TYPES choices on Transaction (add FLW types)
        migrations.AlterField(
            model_name='transaction',
            name='transaction_type',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('mpesa_deposit',      'M-Pesa Deposit'),
                    ('mpesa_withdraw',     'M-Pesa Withdrawal'),
                    ('airtel_deposit',     'Airtel Money Deposit'),
                    ('airtel_withdraw',    'Airtel Money Withdrawal'),
                    ('bank_deposit',       'Bank Deposit'),
                    ('bank_withdraw',      'Bank Withdrawal'),
                    ('flw_card_deposit',   'Card Deposit (Flutterwave)'),
                    ('flw_mobile_deposit', 'Mobile Money Deposit (Flutterwave)'),
                    ('flw_bank_deposit',   'Bank Transfer Deposit (Flutterwave)'),
                    ('flw_bank_payout',    'Bank Payout (Flutterwave)'),
                    ('flw_mobile_payout',  'Mobile Money Payout (Flutterwave)'),
                    ('exchange',           'Currency Exchange'),
                    ('p2p_send',           'Transfer Sent'),
                    ('p2p_receive',        'Transfer Received'),
                ],
            ),
        ),

        # Create FlutterwaveTransaction table
        migrations.CreateModel(
            name='FlutterwaveTransaction',
            fields=[
                ('id',          models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('flw_tx_id',   models.CharField(blank=True, db_index=True, max_length=120)),
                ('tx_ref',      models.CharField(db_index=True, max_length=120, unique=True)),
                ('channel',     models.CharField(max_length=30)),
                ('amount',      models.DecimalField(decimal_places=2, max_digits=14)),
                ('fee',         models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('currency',    models.CharField(default='KES', max_length=3)),
                ('phone',       models.CharField(blank=True, max_length=20)),
                ('direction',   models.CharField(
                    choices=[('in', 'Deposit'), ('out', 'Payout')],
                    default='in', max_length=3,
                )),
                ('status',      models.CharField(
                    choices=[
                        ('pending', 'Pending'), ('completed', 'Completed'),
                        ('failed', 'Failed'), ('refunded', 'Refunded'),
                    ],
                    default='pending', max_length=12,
                )),
                ('raw_payload', models.JSONField(blank=True, null=True)),
                ('created_at',  models.DateTimeField(auto_now_add=True)),
                ('updated_at',  models.DateTimeField(auto_now=True)),
                ('timeout_at',  models.DateTimeField(blank=True, null=True)),
                ('wallet',      models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='flw_transactions',
                    to='wallet.wallet',
                )),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
