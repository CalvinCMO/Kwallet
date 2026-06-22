from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0003_walletuser_session_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='wallet',
            name='is_sandbox',
            field=models.BooleanField(default=True),
        ),
    ]
