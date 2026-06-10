from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0004_alter_feerecord_settlement'),
    ]

    operations = [
        migrations.CreateModel(
            name='PaymentRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.CharField(
                    editable=False, max_length=24, unique=True,
                    help_text='Random slug embedded in the QR URL — /pay/<token>/'
                )),
                ('amount', models.DecimalField(
                    blank=True, decimal_places=2, max_digits=18, null=True,
                    help_text='Leave blank to let the payer choose any amount.'
                )),
                ('note', models.CharField(
                    blank=True, max_length=120,
                    help_text="Short description shown to the payer (e.g. 'Lunch split', 'Invoice #4')."
                )),
                ('single_use', models.BooleanField(
                    default=False,
                    help_text='If True, the QR is disabled after the first successful payment.'
                )),
                ('expires_at', models.DateTimeField(
                    blank=True, null=True,
                    help_text='Optional expiry. Leave blank for a permanent link.'
                )),
                ('status', models.CharField(
                    choices=[
                        ('active',   'Active'),
                        ('paid',     'Paid'),
                        ('expired',  'Expired'),
                        ('disabled', 'Disabled'),
                    ],
                    default='active', max_length=10
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('wallet', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='payment_requests',
                    to='wallet.wallet'
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
