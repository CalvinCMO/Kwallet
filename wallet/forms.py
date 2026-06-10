"""
forms.py — KWallet v2 Forms
"""
from django import forms
from .models import (
    ALL_CURRENCIES, COUNTRY_CHOICES, INTERNATIONAL_CURRENCIES,
    UNIVERSAL_CURRENCIES, FEE_SCHEDULE, calculate_fee
)

# International currency choices (user picks 5 of these)
INTL_CURRENCY_CHOICES = [
    (code, f"{code} — {name}")
    for code, name in ALL_CURRENCIES
    if code in INTERNATIONAL_CURRENCIES
]

# All currency choices for exchange/transfer forms
ALL_CURRENCY_CHOICES = [
    (code, f"{code} — {name}")
    for code, name in ALL_CURRENCIES
]


class RegisterForm(forms.Form):
    full_name   = forms.CharField(max_length=100, label='Full Name')
    phone       = forms.CharField(max_length=15,  label='Phone Number')
    country     = forms.ChoiceField(choices=COUNTRY_CHOICES, label='Country')
    pin         = forms.CharField(widget=forms.PasswordInput, min_length=4, max_length=6, label='PIN')
    pin_confirm = forms.CharField(widget=forms.PasswordInput, label='Confirm PIN')

    # User picks exactly 5 international currencies
    currency_1  = forms.ChoiceField(choices=INTL_CURRENCY_CHOICES, label='International Currency 1')
    currency_2  = forms.ChoiceField(choices=INTL_CURRENCY_CHOICES, label='International Currency 2')
    currency_3  = forms.ChoiceField(choices=INTL_CURRENCY_CHOICES, label='International Currency 3')
    currency_4  = forms.ChoiceField(choices=INTL_CURRENCY_CHOICES, label='International Currency 4')
    currency_5  = forms.ChoiceField(choices=INTL_CURRENCY_CHOICES, label='International Currency 5')

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        from .models import Wallet
        if Wallet.objects.filter(phone=phone).exists():
            raise forms.ValidationError('A wallet with this phone number already exists.')
        return phone

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('pin') != cleaned.get('pin_confirm'):
            raise forms.ValidationError('PINs do not match.')
        # Ensure no duplicate currency choices
        chosen = [
            cleaned.get(f'currency_{i}') for i in range(1, 6)
            if cleaned.get(f'currency_{i}')
        ]
        if len(set(chosen)) != len(chosen):
            raise forms.ValidationError('Please select 5 different currencies.')
        return cleaned

    def get_chosen_currencies(self):
        return [self.cleaned_data[f'currency_{i}'] for i in range(1, 6)]


class LoginForm(forms.Form):
    phone = forms.CharField(max_length=15, label='Phone Number')
    pin   = forms.CharField(widget=forms.PasswordInput, label='PIN')


class ExchangeForm(forms.Form):
    from_currency = forms.ChoiceField(choices=ALL_CURRENCY_CHOICES, label='From')
    to_currency   = forms.ChoiceField(choices=ALL_CURRENCY_CHOICES, label='To')
    amount        = forms.DecimalField(min_value=0.01, decimal_places=2)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('from_currency') == cleaned.get('to_currency'):
            raise forms.ValidationError('Cannot exchange a currency with itself.')
        return cleaned


class P2PTransferForm(forms.Form):
    recipient_phone = forms.CharField(max_length=15, label='Recipient Phone Number')
    currency        = forms.ChoiceField(choices=ALL_CURRENCY_CHOICES)
    amount          = forms.DecimalField(min_value=0.01, decimal_places=2)

    def clean_recipient_phone(self):
        phone = self.cleaned_data['recipient_phone'].strip()
        from .models import Wallet
        if not Wallet.objects.filter(phone=phone).exists():
            raise forms.ValidationError('No wallet found for that phone number.')
        return phone


class MpesaDepositForm(forms.Form):
    amount = forms.DecimalField(min_value=10, decimal_places=2, label='Amount (KES)')
    phone  = forms.CharField(max_length=15, required=False, label='M-Pesa Phone')

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount < 10:
            raise forms.ValidationError('Minimum M-Pesa deposit is KES 10.')
        return amount


class MpesaWithdrawForm(forms.Form):
    amount = forms.DecimalField(min_value=10, decimal_places=2, label='Amount (KES)')
    phone  = forms.CharField(max_length=15, required=False, label='M-Pesa Phone')

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount < 10:
            raise forms.ValidationError('Minimum withdrawal is KES 10.')
        return amount


class AddCurrencyForm(forms.Form):
    """User can add more international currencies after registration."""
    currency = forms.ChoiceField(choices=INTL_CURRENCY_CHOICES, label='Currency to Add')

    def __init__(self, *args, wallet=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.wallet = wallet

    def clean_currency(self):
        currency = self.cleaned_data['currency']
        if self.wallet:
            if self.wallet.currency_balances.filter(currency=currency).exists():
                raise forms.ValidationError(f'You already have a {currency} balance.')
        return currency


# ── QR Payment Forms ────────────────────────────────────────────────────────

from .models import PaymentRequest, PHONE_REGEX
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal


class PaymentRequestForm(forms.ModelForm):
    """
    Used by the wallet owner to create a new payment request / QR code.
    Amount is optional — leave blank for an open-amount request.
    """
    class Meta:
        model  = PaymentRequest
        fields = ['amount', 'note', 'single_use', 'expires_at']
        widgets = {
            'amount':     forms.NumberInput(attrs={
                'placeholder': 'Leave blank to let payer choose',
                'min': '1', 'step': '1',
            }),
            'note':       forms.TextInput(attrs={
                'placeholder': 'e.g. Lunch split, Invoice #4, Rent…',
                'maxlength': '120',
            }),
            'expires_at': forms.DateTimeInput(attrs={
                'type': 'datetime-local',
            }, format='%Y-%m-%dT%H:%M'),
            'single_use': forms.CheckboxInput(),
        }
        labels = {
            'amount':     'Amount (KES) — optional',
            'note':       'Note / Description',
            'single_use': 'One-time use (disable after first payment)',
            'expires_at': 'Expiry date & time — optional',
        }

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is not None and amount <= 0:
            raise forms.ValidationError('Amount must be greater than zero.')
        return amount

    def clean_expires_at(self):
        expires_at = self.cleaned_data.get('expires_at')
        if expires_at and expires_at <= timezone.now():
            raise forms.ValidationError('Expiry must be in the future.')
        return expires_at


class QRPayForm(forms.Form):
    """
    Used by the payer (public, no login) on the /pay/<token>/ page.
    Phone must be a valid Kenyan M-Pesa number.
    Amount is shown but locked if the PaymentRequest has a fixed amount.
    """
    phone  = forms.CharField(
        max_length=10,
        label='Your M-Pesa Phone Number',
        validators=[PHONE_REGEX],
        widget=forms.TextInput(attrs={
            'placeholder': '07XXXXXXXX',
            'inputmode': 'numeric',
            'maxlength': '10',
        }),
    )
    amount = forms.DecimalField(
        max_digits=10, decimal_places=2,
        min_value=Decimal('10'),
        label='Amount (KES)',
        widget=forms.NumberInput(attrs={
            'placeholder': 'Amount in KES',
            'min': '10', 'step': '1',
        }),
    )

    def __init__(self, *args, fixed_amount=None, **kwargs):
        super().__init__(*args, **kwargs)
        if fixed_amount is not None:
            # Pre-fill and lock the amount field
            self.fields['amount'].initial  = fixed_amount
            self.fields['amount'].widget   = forms.NumberInput(attrs={
                'value': str(fixed_amount),
                'readonly': 'readonly',
                'style': 'background:var(--ink10);cursor:not-allowed',
            })
            self.fields['amount'].help_text = 'Amount set by the payee — cannot be changed.'

    def clean_phone(self):
        """
        Validate phone number using the PHONE_REGEX validator.
        PHONE_REGEX is a RegexValidator object from models.py.
        """
        phone = self.cleaned_data.get('phone', '').strip()
        # RegexValidator raises ValidationError if validation fails
        try:
            PHONE_REGEX(phone)
        except forms.ValidationError:
            raise forms.ValidationError('Invalid phone number. Must be 10 digits starting with 07 or 01.')
        return phone
