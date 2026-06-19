# Generated migration — single-device session enforcement + idle timeout tracking
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0002_wallet_kyc_docs_home_currency'),
    ]

    operations = [
        migrations.AddField(
            model_name='walletuser',
            name='active_session_key',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='walletuser',
            name='last_activity',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
