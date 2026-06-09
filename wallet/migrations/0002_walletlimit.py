# Migration: 0002_walletlimit
# Adds the WalletLimit model with USD-equivalent transaction limits.
# - per_transaction_limit_usd : $100 default (max single transaction)
# - base_daily_limit_usd      : $500 default (day-0 daily cap)
# - daily_limit_increment_usd : $10  default (added per full day since registration)

from decimal import Decimal
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='WalletLimit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('per_transaction_limit_usd', models.DecimalField(
                    max_digits=18,
                    decimal_places=4,
                    default=Decimal('100.00'),
                    help_text='Maximum USD-equivalent value allowed for a single transaction.',
                )),
                ('base_daily_limit_usd', models.DecimalField(
                    max_digits=18,
                    decimal_places=4,
                    default=Decimal('500.00'),
                    help_text='Starting daily limit in USD-equivalent (day 0).',
                )),
                ('daily_limit_increment_usd', models.DecimalField(
                    max_digits=18,
                    decimal_places=4,
                    default=Decimal('10.00'),
                    help_text='USD added to the daily cap for each full day since registration.',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('wallet', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='limit',
                    to='wallet.wallet',
                )),
            ],
        ),
    ]
