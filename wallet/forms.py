"""
forms.py — KWallet
"""
from django import forms
from decimal import Decimal
from django.utils import timezone
from django.core.validators import RegexValidator

PHONE_REGEX = RegexValidator(
    regex=r'^(07|01)\d{8}$',
    message='Enter a valid Kenyan mobile number (10 digits, starting with 07 or 01).',
)

MAX_CURRENCIES = 10


# ── Registration ─────────────────────────────────────────────────────────────

class RegisterForm(forms.Form):
    first_name  = forms.CharField(max_length=80, label='First Name')
    last_name   = forms.CharField(max_length=80, label='Last Name')
    phone       = forms.CharField(max_length=20, label='Phone Number')
    country     = forms.ChoiceField(choices=[
        ('KE','Kenya'),('TZ','Tanzania'),('UG','Uganda'),
        ('RW','Rwanda'),('ET','Ethiopia'),('NG','Nigeria'),('GH','Ghana'),
    ], label='Country')
    pin         = forms.CharField(widget=forms.PasswordInput, min_length=6, max_length=6, label='PIN')
    pin_confirm = forms.CharField(widget=forms.PasswordInput, label='Confirm PIN')

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        from .models import WalletUser
        if WalletUser.objects.filter(phone=phone).exists():
            raise forms.ValidationError('A wallet with this phone number already exists.')
        return phone

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('pin') != cleaned.get('pin_confirm'):
            raise forms.ValidationError('PINs do not match.')
        return cleaned


# ── Login ────────────────────────────────────────────────────────────────────

class LoginForm(forms.Form):
    phone = forms.CharField(max_length=20, label='Phone Number')
    pin   = forms.CharField(widget=forms.PasswordInput, label='PIN')


# ── Exchange ─────────────────────────────────────────────────────────────────

class ExchangeForm(forms.Form):
    from_currency = forms.CharField(max_length=3, label='From Currency')
    to_currency   = forms.CharField(max_length=3, label='To Currency')
    amount        = forms.DecimalField(min_value=Decimal('0.01'), decimal_places=4, label='Amount')

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('from_currency') == cleaned.get('to_currency'):
            raise forms.ValidationError('Cannot exchange a currency with itself.')
        return cleaned


# ── P2P Transfer ─────────────────────────────────────────────────────────────

class P2PTransferForm(forms.Form):
    recipient_phone = forms.CharField(max_length=20, label='Recipient Phone Number')
    currency        = forms.CharField(max_length=3, label='Currency')
    amount          = forms.DecimalField(min_value=Decimal('0.01'), decimal_places=4, label='Amount')

    def clean_recipient_phone(self):
        phone = self.cleaned_data['recipient_phone'].strip()
        from .models import Wallet
        if not Wallet.objects.filter(phone=phone).exists():
            raise forms.ValidationError('No wallet found for that phone number.')
        return phone


# ── M-Pesa / Airtel ──────────────────────────────────────────────────────────

class MobileDepositForm(forms.Form):
    amount = forms.DecimalField(min_value=Decimal('10'), decimal_places=2, label='Amount (KES)')
    phone  = forms.CharField(max_length=20, required=False, label='Phone Number')

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount < 10:
            raise forms.ValidationError('Minimum deposit is KES 10.')
        return amount


class MobileWithdrawForm(forms.Form):
    amount = forms.DecimalField(min_value=Decimal('50'), decimal_places=2, label='Amount (KES)')
    phone  = forms.CharField(max_length=20, required=False, label='Phone Number')


# ── QR Payment ───────────────────────────────────────────────────────────────

class QRPayForm(forms.Form):
    """Public (no-login) pay page — used by the payer."""
    rail = forms.ChoiceField(
        choices=[('mpesa', 'M-Pesa'), ('airtel', 'Airtel Money')],
        label='Payment Method',
    )
    phone = forms.CharField(
        max_length=13,
        label='Your Phone Number',
        widget=forms.TextInput(attrs={
            'placeholder': '07XXXXXXXX',
            'inputmode': 'numeric',
        }),
    )
    amount = forms.DecimalField(
        max_digits=10, decimal_places=2,
        min_value=Decimal('10'),
        label='Amount (KES)',
        required=False,
    )

    def __init__(self, *args, fixed_amount=None, **kwargs):
        super().__init__(*args, **kwargs)
        if fixed_amount is not None:
            self.fields['amount'].initial = fixed_amount
            self.fields['amount'].widget = forms.NumberInput(attrs={
                'value': str(fixed_amount),
                'readonly': 'readonly',
            })
            self.fields['amount'].required = False


# ── PIN Reset ──────────────────────────────────────────────────────────────────

class PinResetRequestForm(forms.Form):
    phone = forms.CharField(max_length=20, label='Phone Number')


class PinResetVerifyForm(forms.Form):
    code = forms.CharField(
        max_length=6, min_length=6, label='Verification Code',
        widget=forms.TextInput(attrs={'placeholder': '000000', 'autocomplete': 'one-time-code'})
    )


class PinResetSetForm(forms.Form):
    pin = forms.CharField(widget=forms.PasswordInput, min_length=6, label='New PIN')
    pin_confirm = forms.CharField(widget=forms.PasswordInput, label='Confirm New PIN')

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('pin') != cleaned.get('pin_confirm'):
            raise forms.ValidationError('PINs do not match.')
        return cleaned
