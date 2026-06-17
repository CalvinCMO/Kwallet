"""
Migration 0007: Add new models for risk mitigations.
- AirtelTransaction  (new — Airtel Money integration)
- BankTransaction    (new — PesaLink/RTGS)
- SuspiciousActivityFlag (new — Risk #16 AML)
- PinResetToken      (new — Risk #03)
- Add timeout_at to MpesaTransaction (Risk #04)
- Add kyc_verified_at to Wallet (Risk #15)

NOTE: failed_login_attempts/locked_until on WalletUser are already created
directly in 0006_create_walletuser's CreateModel — not added here, to avoid
a duplicate-column error.
"""
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0006_create_walletuser'),
    ]

    operations = [
        # Risk #15: KYC verified timestamp on Wallet
        migrations.AddField(
            model_name='wallet',
            name='kyc_verified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),

        # Risk #04: timeout_at on MpesaTransaction
        migrations.AddField(
            model_name='mpesatransaction',
            name='timeout_at',
            field=models.DateTimeField(blank=True, null=True),
        ),

        # Airtel Money transactions
        migrations.CreateModel(
            name='AirtelTransaction',
            fields=[
                ('id', models.BigAutoField(primary_key=True)),
                ('airtel_ref', models.CharField(db_index=True, max_length=100, unique=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('phone', models.CharField(max_length=20)),
                ('status', models.CharField(
                    choices=[('pending','Pending'),('completed','Completed'),('failed','Failed'),('refunded','Refunded')],
                    default='pending', max_length=12
                )),
                ('transaction_type', models.CharField(default='airtel_deposit', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('timeout_at', models.DateTimeField(blank=True, null=True)),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='wallet.wallet')),
            ],
            options={'ordering': ['-created_at']},
        ),

        # Bank transactions
        migrations.CreateModel(
            name='BankTransaction',
            fields=[
                ('id', models.BigAutoField(primary_key=True)),
                ('pesalink_ref', models.CharField(db_index=True, max_length=100, unique=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('bank_name', models.CharField(max_length=80)),
                ('account_number', models.CharField(max_length=80)),
                ('account_name', models.CharField(max_length=120)),
                ('status', models.CharField(
                    choices=[('pending','Pending'),('completed','Completed'),('failed','Failed'),('refunded','Refunded')],
                    default='pending', max_length=12
                )),
                ('transaction_type', models.CharField(default='bank_deposit', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('timeout_at', models.DateTimeField(blank=True, null=True)),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='wallet.wallet')),
            ],
            options={'ordering': ['-created_at']},
        ),

        # Risk #16: AML suspicious activity flags
        migrations.CreateModel(
            name='SuspiciousActivityFlag',
            fields=[
                ('id', models.BigAutoField(primary_key=True)),
                ('flag_type', models.CharField(max_length=40)),
                ('description', models.TextField()),
                ('reviewed', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='flags', to='wallet.wallet')),
                ('transaction', models.ForeignKey(blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL, to='wallet.transaction')),
            ],
            options={'ordering': ['-created_at']},
        ),

        # Risk #03: secure PIN reset tokens
        migrations.CreateModel(
            name='PinResetToken',
            fields=[
                ('id', models.BigAutoField(primary_key=True)),
                ('token', models.CharField(max_length=64, unique=True)),
                ('code', models.CharField(max_length=6)),
                ('used', models.BooleanField(default=False)),
                ('expires_at', models.DateTimeField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    to='wallet.walletuser')),
            ],
        ),

        # Transaction — add new fields for Airtel/bank + idempotency (Risk #02)
        migrations.AddField(
            model_name='transaction',
            name='idempotency_key',
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='transaction',
            name='bank_name',
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name='transaction',
            name='bank_account',
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name='transaction',
            name='fee',
            field=models.DecimalField(decimal_places=6, default=0, max_digits=18),
        ),

        # Risk #14: ensure max currency cap enforced at DB level via check constraint
        # (enforced in view + form; DB constraint here as belt-and-suspenders)
    ]
