# Migration: 0003_settlement_models
# Adds the three real-money plumbing models:
#   - CompanyAccount  : real-world account registry (client float & company revenue)
#   - FeeSettlement   : batch fee sweep records
#   - PoolLedger      : immutable double-entry ledger of every real-money movement
# Also adds FeeRecord.settlement FK so fee records can be linked to their sweep.

from decimal import Decimal
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0002_walletlimit'),
    ]

    operations = [

        # ── CompanyAccount ────────────────────────────────────────────────────
        migrations.CreateModel(
            name='CompanyAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True,
                    help_text="Human label, e.g. 'M-Pesa Client Float KES'")),
                ('account_type', models.CharField(max_length=20, choices=[
                    ('client_float',    'Client Float (Segregated)'),
                    ('company_revenue', 'Company Revenue'),
                ])),
                ('rail', models.CharField(max_length=20, choices=[
                    ('mpesa',      'M-Pesa Paybill / Till'),
                    ('bank_kes',   'Bank — KES'),
                    ('bank_usd',   'Bank — USD'),
                    ('bank_other', 'Bank — Other Currency'),
                    ('psp',        'PSP / Payment Partner'),
                ])),
                ('currency', models.CharField(max_length=3)),
                ('identifier', models.CharField(max_length=100,
                    help_text='Paybill/till number, IBAN, account number, etc.')),
                ('ledger_balance', models.DecimalField(max_digits=18, decimal_places=4,
                    default=Decimal('0.0000'))),
                ('is_active', models.BooleanField(default=True)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ['account_type', 'currency', 'name']},
        ),

        # ── FeeSettlement ─────────────────────────────────────────────────────
        migrations.CreateModel(
            name='FeeSettlement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID')),
                ('reference', models.CharField(max_length=40, unique=True, editable=False)),
                ('currency', models.CharField(max_length=3)),
                ('total_fees', models.DecimalField(max_digits=18, decimal_places=4,
                    help_text='Sum of all FeeRecord amounts in this batch.')),
                ('fee_count', models.IntegerField(default=0,
                    help_text='Number of FeeRecord rows included.')),
                ('from_account', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='sweeps_out',
                    to='wallet.companyaccount',
                    help_text='Client float account being debited.',
                )),
                ('to_account', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='sweeps_in',
                    to='wallet.companyaccount',
                    help_text='Company revenue account being credited.',
                )),
                ('status', models.CharField(max_length=10, default='pending', choices=[
                    ('pending',   'Pending'),
                    ('completed', 'Completed'),
                    ('failed',    'Failed'),
                ])),
                ('failure_reason', models.TextField(blank=True)),
                ('initiated_by', models.CharField(max_length=100, default='system')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(null=True, blank=True)),
            ],
            options={'ordering': ['-created_at']},
        ),

        # ── PoolLedger ────────────────────────────────────────────────────────
        migrations.CreateModel(
            name='PoolLedger',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID')),
                ('account', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='ledger_entries',
                    to='wallet.companyaccount',
                )),
                ('entry_type', models.CharField(max_length=20, choices=[
                    ('deposit_in',       'Deposit In'),
                    ('withdrawal_out',   'Withdrawal Out'),
                    ('fee_sweep_out',    'Fee Sweep — Debit Client Float'),
                    ('fee_sweep_in',     'Fee Sweep — Credit Company Revenue'),
                    ('fx_rebalance_out', 'FX Rebalance — Out'),
                    ('fx_rebalance_in',  'FX Rebalance — In'),
                    ('adjustment',       'Manual Adjustment'),
                ])),
                ('amount', models.DecimalField(max_digits=18, decimal_places=4,
                    help_text='Always positive. Direction implied by entry_type.')),
                ('currency', models.CharField(max_length=3)),
                ('balance_after', models.DecimalField(max_digits=18, decimal_places=4,
                    help_text='CompanyAccount.ledger_balance after this entry.')),
                ('transaction', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='pool_entries',
                    to='wallet.transaction',
                )),
                ('settlement', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='pool_entries',
                    to='wallet.feesettlement',
                )),
                ('note', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.CharField(max_length=100, default='system',
                    help_text="'system', 'sweep_job', or admin username.")),
            ],
            options={'ordering': ['-created_at']},
        ),

        # ── FeeRecord.settlement FK ───────────────────────────────────────────
        migrations.AddField(
            model_name='feerecord',
            name='settlement',
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='fee_records',
                to='wallet.feesettlement',
                help_text='Set when this fee has been swept to the revenue account.',
            ),
        ),
    ]
