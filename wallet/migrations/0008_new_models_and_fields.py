"""
Migration 0008: All risk-mitigation additions and new integrations.

Existing tables from 0001-0005 (confirmed fields):
  Wallet            : wallet_id(PK char), phone, pin_hash, country, kyc_status,
                      created_at, legacy_user_id(plain int, no relation)
  Transaction       : id, transaction_type, amount, fee, currency, status,
                      details, reference(unique), created_at, updated_at, wallet(FK)
  MpesaTransaction  : id, phone, amount, checkout_request_id(unique),
                      merchant_request_id, direction, status, result_code,
                      result_desc, mpesa_receipt, created_at, updated_at, wallet(FK)
  FeeRecord         : id, amount, currency, fee_type, collected_at, transaction(O2O), wallet(FK), settlement(FK)
  PaymentMethod     : id, rail, label, identifier, currency, country,
                      is_verified, is_default, added_at, wallet(FK)
  CurrencyBalance   : id, currency, balance, last_updated, wallet(FK)
  WalletLimit       : id, per_transaction_limit_usd, base_daily_limit_usd,
                      daily_limit_increment_usd, created_at, updated_at, wallet(O2O)
  CompanyAccount    : id, name, account_type, rail, currency, identifier,
                      ledger_balance, is_active, notes, created_at, updated_at
  FeeSettlement     : id, reference, currency, total_fees, fee_count,
                      from_account(FK), to_account(FK), status, failure_reason,
                      initiated_by, created_at, completed_at
  PoolLedger        : id, account(FK), entry_type, amount, currency,
                      balance_after, transaction(FK), settlement(FK),
                      note, created_at, created_by
  PaymentRequest    : id, token, amount, note, single_use, expires_at,
                      status, created_at, updated_at, wallet(FK)

This migration ONLY adds fields/tables that do NOT already exist above.
PinResetToken is created in 0007_risk_mitigations, not here (was duplicated).
"""

from decimal import Decimal
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0007_risk_mitigations'),
    ]

    operations = [

        # ── Wallet: add wallet_user FK (new, nullable) ────────────────────────
        # This is the only user relation now; the old auth.User FK was
        # removed from the model (Django forbids referencing a swapped-out
        # user model) and replaced with a plain legacy_user_id integer.
        migrations.AddField(
            model_name='wallet',
            name='wallet_user',
            field=models.OneToOneField(
                null=True, blank=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='wallet',
                to='wallet.walletuser',
            ),
        ),

        # ── Wallet: wallet_id_str — KW... human-readable ID ──────────────────
        # (0001 uses wallet_id as PK char; we add wallet_id_str for new code)
        migrations.AddField(
            model_name='wallet',
            name='wallet_id_str',
            field=models.CharField(max_length=20, unique=True, null=True, blank=True),
        ),

        # ── Wallet: home_currency ─────────────────────────────────────────────
        migrations.AddField(
            model_name='wallet',
            name='home_currency',
            field=models.CharField(max_length=3, default='KES'),
        ),

        # NOTE: kyc_verified_at added in 0007_risk_mitigations, not here
        # (was previously duplicated in both migrations).

        # ── Wallet: updated_at ────────────────────────────────────────────────
        migrations.AddField(
            model_name='wallet',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, null=True),
        ),

        # ── MpesaTransaction: timeout_at (Risk #04) ───────────────────────────

        # ── MpesaTransaction: transaction_type ────────────────────────────────
        # (0001 used 'direction' in/out; new code uses transaction_type)
        migrations.AddField(
            model_name='mpesatransaction',
            name='transaction_type',
            field=models.CharField(max_length=20, default='mpesa_deposit'),
        ),

        # ── Transaction: idempotency_key (Risk #02) ───────────────────────────
        # (0001 has 'reference' unique; idempotency_key is separate)

        # ── Transaction: external_ref ─────────────────────────────────────────
        migrations.AddField(
            model_name='transaction',
            name='external_ref',
            field=models.CharField(max_length=120, blank=True, db_index=True),
        ),

        # ── Transaction: bank_name ────────────────────────────────────────────

        # ── Transaction: bank_account ─────────────────────────────────────────

        # ── Transaction: recipient_wallet (Risk #06 P2P masking) ─────────────
        migrations.AddField(
            model_name='transaction',
            name='recipient_wallet',
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='received_transactions',
                to='wallet.wallet',
            ),
        ),

        # ── WalletLimit: KES-based limit fields (alongside existing USD ones) ─
        migrations.AddField(
            model_name='walletlimit',
            name='daily_withdraw_kes',
            field=models.DecimalField(max_digits=12, decimal_places=2,
                                      default=Decimal('70000')),
        ),
        migrations.AddField(
            model_name='walletlimit',
            name='per_txn_max_kes',
            field=models.DecimalField(max_digits=12, decimal_places=2,
                                      default=Decimal('150000')),
        ),
        migrations.AddField(
            model_name='walletlimit',
            name='monthly_limit_kes',
            field=models.DecimalField(max_digits=12, decimal_places=2,
                                      default=Decimal('1000000')),
        ),

        # ── CompanyAccount: balance (simple alias of ledger_balance for new code)
        # 0003 has ledger_balance; add 'balance' as the field new models.py uses
        migrations.AddField(
            model_name='companyaccount',
            name='balance',
            field=models.DecimalField(max_digits=18, decimal_places=6,
                                      default=Decimal('0')),
        ),

        # ── PoolLedger: reference (new code uses .reference, 0003 used .note) ─
        migrations.AddField(
            model_name='poolledger',
            name='reference',
            field=models.CharField(max_length=120, blank=True),
        ),

        # ── NEW MODEL: AirtelTransaction ──────────────────────────────────────

        # ── NEW MODEL: BankTransaction ────────────────────────────────────────

        # ── NEW MODEL: SuspiciousActivityFlag (Risk #16 AML) ──────────────────

        # ── NEW MODEL: PinResetToken (Risk #03) ───────────────────────────────
        # NOTE: PinResetToken is created in 0007_risk_mitigations, not here
        # (was previously duplicated in both migrations).

        # ── NEW MODEL: QRPaymentRequest ───────────────────────────────────────
        # (0005 created PaymentRequest; QRPaymentRequest is a new separate table
        #  used by the new views — the old PaymentRequest table is left intact)
        migrations.CreateModel(
            name='QRPaymentRequest',
            fields=[
                ('id',         models.BigAutoField(primary_key=True, serialize=False)),
                ('token',      models.CharField(max_length=64, unique=True, db_index=True)),
                ('amount',     models.DecimalField(max_digits=12, decimal_places=2,
                               null=True, blank=True)),
                ('note',       models.CharField(max_length=120, blank=True)),
                ('single_use', models.BooleanField(default=False)),
                ('status',     models.CharField(max_length=10, default='active',
                    choices=[('active','Active'),('paid','Paid'),
                             ('expired','Expired'),('disabled','Disabled')])),
                ('expires_at', models.DateTimeField(null=True, blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('wallet',     models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='qr_requests',
                    to='wallet.wallet')),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
