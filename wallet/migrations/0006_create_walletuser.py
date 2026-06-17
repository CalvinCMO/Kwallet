"""
Migration 0006: (historical no-op)

WalletUser used to be created here, but that's exactly what broke
production: Django resolves admin/auth's swappable_dependency on
AUTH_USER_MODEL to wallet's FIRST migration, not to wherever the model
happens to be defined. With WalletUser created in 0006, admin.0001_initial
ran right after wallet.0001_initial and tried to FK into a WalletUser
table that didn't exist yet -> "relation does not exist".

WalletUser is now created directly in 0001_initial. This migration is
kept as a harmless no-op so the migration history/numbering of 0007+
doesn't have to change.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0005_paymentrequest'),
    ]

    operations = []
