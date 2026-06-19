# Generated migration — KYC document fields + home_currency default change
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='wallet',
            name='kyc_id_front',
            field=models.ImageField(blank=True, null=True, upload_to='kyc/id/'),
        ),
        migrations.AddField(
            model_name='wallet',
            name='kyc_id_back',
            field=models.ImageField(blank=True, null=True, upload_to='kyc/id/'),
        ),
        migrations.AddField(
            model_name='wallet',
            name='kyc_selfie',
            field=models.ImageField(blank=True, null=True, upload_to='kyc/selfie/'),
        ),
        migrations.AddField(
            model_name='wallet',
            name='kyc_full_name',
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name='wallet',
            name='kyc_id_number',
            field=models.CharField(blank=True, max_length=60),
        ),
        migrations.AddField(
            model_name='wallet',
            name='kyc_dob',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='wallet',
            name='home_currency',
            field=models.CharField(blank=True, default='', max_length=3),
        ),
    ]
