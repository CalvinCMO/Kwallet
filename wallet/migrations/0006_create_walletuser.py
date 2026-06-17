"""
Migration 0006: Create WalletUser custom auth model.

The original 0001 used swappable_dependency(settings.AUTH_USER_MODEL)
pointing to auth.User. We now introduce wallet.WalletUser. This migration
only creates the table so 0007 can reference it safely in the graph.

IMPORTANT: after applying this migration set settings.AUTH_USER_MODEL
= 'wallet.WalletUser' and run migrate.
"""
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0005_paymentrequest'),
    ]

    operations = [
        migrations.CreateModel(
            name='WalletUser',
            fields=[
                ('id',                    models.BigAutoField(primary_key=True, serialize=False)),
                ('password',              models.CharField(max_length=255)),
                ('last_login',            models.DateTimeField(null=True, blank=True)),
                ('phone',                 models.CharField(max_length=20, unique=True)),
                ('first_name',            models.CharField(max_length=80, blank=True)),
                ('last_name',             models.CharField(max_length=80, blank=True)),
                ('is_active',             models.BooleanField(default=True)),
                ('is_staff',              models.BooleanField(default=False)),
                ('is_superuser',          models.BooleanField(default=False)),
                ('date_joined',           models.DateTimeField(default=django.utils.timezone.now)),
                # Risk #03: brute-force lockout fields
                ('failed_login_attempts', models.PositiveIntegerField(default=0)),
                ('locked_until',          models.DateTimeField(null=True, blank=True)),
            ],
            options={'verbose_name': 'User'},
        ),
    ]
