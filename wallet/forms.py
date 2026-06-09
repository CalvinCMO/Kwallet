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
    amount        = forms.DecimalField(min_value=0.01, decimal_places=4)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('from_currency') == cleaned.get('to_currency'):
            raise forms.ValidationError('Cannot exchange a currency with itself.')
        return cleaned


class P2PTransferForm(forms.Form):
    recipient_phone = forms.CharField(max_length=15, label='Recipient Phone Number')
    currency        = forms.ChoiceField(choices=ALL_CURRENCY_CHOICES)
    amount          = forms.DecimalField(min_value=0.01, decimal_places=4)

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
